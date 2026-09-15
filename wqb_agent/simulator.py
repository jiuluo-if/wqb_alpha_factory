"""ROLE: CORE
AGENT_RELEVANCE: HIGH
PURPOSE: Execute validated experiments through the client and collect evidence.
READ WHEN: changing Simulation dispatch, polling, or result parsing.
DO NOT USE FOR: selecting research hypotheses or creating a second POST path.

Three-window rolling simulation dispatcher.

Keeps up to `max_concurrent` simulations in flight and refills the window as
soon as one completes (FIRST_COMPLETED), so the 3 windows never idle.

Safety semantics (rolling executor, three windows):

- A definite rejection (syntax/settings, 400/422) marks the experiment FAILED
  and does NOT pause dispatch — it is a property of the expression.
- Polling timeout / platform 5xx / network errors for a known progress URL are
  handled by REPOLL: the same remote Simulation is polled again, up to
  `replace_attempts` times with growing backoff. No replacement POST is made;
  an exhausted poll budget settles as UNKNOWN and remains eligible for
  read-only reconciliation.
- An authentication rejection (401/403) marks FAILED and PAUSES dispatch:
  retrying the same window is pointless until credentials are fixed.
- Any other local exception (network / read error) marks UNKNOWN too — a
  local error does not prove the POST never happened.
"""

import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from .client import (
    WQBAuthError,
    WQBError,
    WQBNotFoundError,
    WQBRateLimitError,
    WQBRejectedError,
    WQBSimulationError,
    WQBSubmitUnknownError,
    WQBTimeoutError,
)
from .experiment import UNKNOWN_STATUSES
from .metrics import check_health as _check_health
from .metrics import extract_metrics as _extract_metrics
from .yearly import build_yearly_evidence


class Simulator:
    def __init__(self, client, max_concurrent=3, poll_timeout_sec=1500,
                 replace_attempts=3, replace_backoff_sec=60, yearly_policy=None):
        self.client = client
        self.max_concurrent = max_concurrent
        self.poll_timeout_sec = poll_timeout_sec
        self.replace_attempts = max(1, replace_attempts)
        self.replace_backoff_sec = max(0, replace_backoff_sec)
        self.yearly_policy = dict(yearly_policy or {})
        self.stop_dispatch = False
        self.paused_reason = None

    def run(self, experiments, on_complete=None, on_update=None):
        """Run experiments and optionally notify as each one settles.

        ``on_complete`` is deliberately invoked from the dispatcher thread
        after a worker has produced a terminal result.  This makes the real
        platform result visible immediately (rather than after the whole
        batch), while keeping state mutation and reflection in the caller.
        Terminal callbacks are the acknowledgement boundary for durable local
        evidence.  A terminal checkpoint notification is therefore emitted
        only after ``on_complete`` returns successfully; callback failures are
        propagated instead of being downgraded to a transport result.
        """
        self.stop_dispatch = False
        self.paused_reason = None
        pending = deque(experiments)
        completed = []
        in_flight = {}

        with ThreadPoolExecutor(
            max_workers=self.max_concurrent, thread_name_prefix="wqb-sim"
        ) as executor:
            def fill_window():
                while (
                    pending
                    and len(in_flight) < self.max_concurrent
                    and not self.stop_dispatch
                ):
                    exp = pending.popleft()
                    in_flight[executor.submit(self._simulate_one, exp, on_update)] = exp

            fill_window()
            while in_flight:
                done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for future in done:
                    exp = in_flight.pop(future)
                    try:
                        future.result()
                    except Exception as exc:
                        # 未分类的本地异常不证明 POST 未发生；暂停新派发。
                        exp.status = "UNKNOWN"
                        exp.error = f"UNKNOWN_LOCAL {type(exc).__name__}: {exc}"
                    if self._pauses_dispatch(exp):
                        self.stop_dispatch = True
                        self.paused_reason = f"{exp.status}:{exp.id}"
                    if on_complete is not None:
                        acknowledged = on_complete(exp)
                        if acknowledged is False:
                            raise RuntimeError(
                                f"terminal evidence acknowledgement rejected: {exp.id}"
                            )
                    if exp.status in {"DONE", "FAILED"} and on_update is not None:
                        on_update(exp)
                    completed.append(exp)
                fill_window()

        if self.paused_reason:
            # 注意：不要用非 ASCII 前缀符号（如 U+23F8），Windows GBK 控制台
            # 会抛 UnicodeEncodeError 导致派发线程崩溃（基线测试已复现）。
            print(f"[PAUSED] 暂停新派发：{self.paused_reason}；先只读审计后继续。")
        return completed

    @staticmethod
    def _pauses_dispatch(exp):
        # A known progress URL is a durable remote identity.  A polling
        # failure for that job can be reconciled read-only on the next pass;
        # it must not block unrelated proposals that have not been submitted.
        # Submission ambiguity without a URL still pauses dispatch because a
        # second POST could duplicate an unknown remote job.
        if exp.status in UNKNOWN_STATUSES:
            return exp.status == "SUBMIT_UNKNOWN" or not exp.progress_url
        if exp.status == "FAILED" and exp.error and (
            "401" in exp.error or "403" in exp.error
        ):
            return True
        return False

    def _simulate_one(self, experiment, on_update=None):
        """Run one durable experiment without issuing duplicate POSTs.

        A retry after a *known* progress URL only re-polls that same BRAIN
        job.  An ambiguous submit becomes ``SUBMIT_UNKNOWN`` and is retained
        for reconciliation instead of being re-submitted.
        """
        _t0 = time.time()
        recoverable = (WQBRateLimitError, WQBTimeoutError, WQBSimulationError)

        def persist():
            # DONE/FAILED updates are checkpoint notifications.  They must be
            # deferred until the dispatcher has received the successful
            # canonical evidence acknowledgement from ``on_complete``.
            if on_update is not None and experiment.status not in {"DONE", "FAILED"}:
                on_update(experiment)

        try:
            # A process that died after writing SUBMITTING cannot establish
            # whether the POST ran.  Do not guess and do not spend the budget
            # twice; leave it for explicit reconciliation.
            if experiment.status in ("SUBMITTING", "SUBMIT_UNKNOWN"):
                experiment.status = "SUBMIT_UNKNOWN"
                experiment.error = experiment.error or "submission outcome unknown after interrupted POST"
                persist()
                return experiment

            if experiment.progress_url:
                experiment.status = "RUNNING"
                persist()
            else:
                experiment.status = "SUBMITTING"
                experiment.submission_started_at = time.time()
                persist()
                for _submit_attempt in range(1, self.replace_attempts + 1):
                    try:
                        try:
                            experiment.progress_url = self.client.submit_simulation(
                                experiment.expression,
                                experiment.settings,
                                idempotency_key=experiment.submission_fingerprint,
                            )
                        except TypeError as exc:
                            # Test doubles and pre-fingerprint clients keep the
                            # safe caller-side checkpoint semantics.
                            if "idempotency_key" not in str(exc):
                                raise
                            experiment.progress_url = self.client.submit_simulation(
                                experiment.expression, experiment.settings
                            )
                        experiment.status = "RUNNING"
                        persist()
                        break
                    except WQBRateLimitError as exc:
                        # Do not infer write safety from a transport code.  A
                        # retry is legal only when the client/API contract has
                        # explicitly established it; this generic client does
                        # not make that claim.
                        experiment.error = f"{type(exc).__name__}: {exc}"
                        experiment.status = "SUBMIT_UNKNOWN"
                        persist()
                        return experiment
                    except WQBSubmitUnknownError as exc:
                        experiment.error = f"{type(exc).__name__}: {exc}"
                        experiment.status = "SUBMIT_UNKNOWN"
                        persist()
                        return experiment
                    except WQBRejectedError as exc:
                        experiment.error = f"{type(exc).__name__}: {exc}"
                        experiment.status = "FAILED"
                        persist()
                        return experiment
                    except WQBAuthError as exc:
                        experiment.error = f"{type(exc).__name__}: {exc}"
                        experiment.status = "FAILED"
                        persist()
                        return experiment
                    except WQBError as exc:
                        experiment.error = f"{type(exc).__name__}: {exc}"
                        experiment.status = "SUBMIT_UNKNOWN"
                        persist()
                        return experiment
                    except Exception as exc:
                        msg = f"{type(exc).__name__}: {exc}"
                        experiment.error = msg
                        experiment.status = (
                            "FAILED" if "Simulation rejected" in msg else "SUBMIT_UNKNOWN"
                        )
                        persist()
                        return experiment

            # A known progress URL is always polled again.  Poll failures no
            # longer trigger a replacement POST: the original BRAIN job owns
            # this budget slot and can be resumed safely after a crash.
            for attempt in range(1, self.replace_attempts + 1):
                try:
                    progress_url = experiment.progress_url
                    def callback(elapsed, polls, code):
                        print(
                            f"[POLL] {experiment.id} elapsed={elapsed:.0f}s polls={polls} status={code}"
                        )
                    try:
                        alpha_id = self.client.poll_progress(
                            progress_url,
                            timeout_sec=self.poll_timeout_sec,
                            progress_callback=callback,
                        )
                    except TypeError as exc:
                        if "progress_callback" not in str(exc):
                            raise
                        alpha_id = self.client.poll_progress(
                            progress_url, timeout_sec=self.poll_timeout_sec
                        )
                    payload = self.client.get_alpha(alpha_id)
                    experiment.alpha_id = alpha_id
                    experiment.metrics = _extract_metrics(payload)
                    aggregates = getattr(self.client, "get_aggregates", None)
                    if callable(aggregates):
                        try:
                            experiment.yearly_evidence = build_yearly_evidence(
                                aggregates(alpha_id),
                                min_sharpe=self.yearly_policy.get("min_sharpe", 0.0),
                                min_fitness=self.yearly_policy.get("min_fitness", 0.0),
                                max_turnover=self.yearly_policy.get("max_turnover"),
                                min_years=self.yearly_policy.get("min_years", 1),
                            )
                        except Exception as exc:
                            # Annual evidence is read-only advisory evidence;
                            # its outage must not turn a completed Simulation
                            # into UNKNOWN or trigger a second POST.
                            experiment.yearly_evidence = {
                                "status": "UNKNOWN",
                                "source": "BRAIN /alphas/{id}/aggregates",
                                "years": [],
                                "stable": None,
                                "reason": f"aggregates unavailable: {type(exc).__name__}",
                            }
                    experiment.status = "DONE"
                    try:
                        health = _check_health(payload)
                        experiment.health = health
                    except Exception:
                        experiment.health = None
                    persist()
                    return experiment
                except (WQBNotFoundError, WQBRejectedError) as exc:
                    # 404=NOT_FOUND / 400/422/403=REJECTED：平台明确拒绝，
                    # 不触发未知预算占用；标 FAILED 且不暂停派发。
                    experiment.error = f"{type(exc).__name__}: {exc}"
                    experiment.status = "FAILED"
                    persist()
                    return experiment
                except recoverable as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    if attempt < self.replace_attempts:
                        delay = self.replace_backoff_sec * attempt
                        print(
                            f"[REPOLL] {experiment.id} 第{attempt}/{self.replace_attempts}"
                            f"次轮询失败，{delay}s 后重试同一 simulation：{last_error[:90]}"
                        )
                        time.sleep(delay)
                        continue
                    # 同一远程任务的重轮询预算耗尽：保留 UNKNOWN，等待
                    # 只读对账；不把轮询失败误写成新的 Simulation。
                    experiment.error = last_error
                    experiment.status = "UNKNOWN"
                    persist()
                    return experiment
                except WQBRejectedError as exc:
                    # 明确拒绝（400/422/403/404）：表达式/设置/字段问题，方向可判，
                    # 标 FAILED 且不暂停派发（是该表达式自身的属性）。
                    experiment.error = f"{type(exc).__name__}: {exc}"
                    experiment.status = "FAILED"
                    persist()
                    return experiment
                except WQBAuthError as exc:
                    experiment.error = f"{type(exc).__name__}: {exc}"
                    experiment.status = "FAILED"
                    persist()
                    return experiment
                except WQBError as exc:
                    # 其余分类错误（如数据缺失）：POST 可能已发生但结果未知，
                    # 标 UNKNOWN 等只读对账。
                    experiment.error = f"{type(exc).__name__}: {exc}"
                    experiment.status = "UNKNOWN"
                    persist()
                    return experiment
                except Exception as exc:
                    msg = f"{type(exc).__name__}: {exc}"
                    experiment.error = msg
                    if "Simulation rejected" in msg:
                        # 兼容旧 client 的字符串式拒绝信号（400/422）。
                        experiment.status = "FAILED"
                    else:
                        # 网络/轮询/读取异常：无法证明 POST 是否发生。
                        experiment.status = "UNKNOWN"
                    persist()
                    return experiment
            return experiment
        finally:
            # 真实计时：提交开始 → 定论（含替换重试退避等待），程序自身记录，
            # 不依赖外部检测进程。写入 experiment 供 trajectory / sims_results
            # 输出。
            experiment.elapsed_sec = time.time() - _t0
