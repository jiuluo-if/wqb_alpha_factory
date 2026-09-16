"""ROLE: CORE
AGENT_RELEVANCE: HIGH
PURPOSE: Execute validated experiments through the client and collect evidence.
READ WHEN: changing Simulation dispatch, polling, or result parsing.
DO NOT USE FOR: selecting research hypotheses or creating a second POST path.

Bounded concurrent Simulation dispatcher.

Keeps up to `max_concurrent` simulations in flight and refills the window as
soon as one completes (FIRST_COMPLETED), so the 3 windows never idle.

Safety semantics (rolling executor, three windows):

- A definite rejection (syntax/settings, 400/422) marks the experiment FAILED
  and does NOT pause dispatch — it is a property of the expression.
- Polling timeout / platform 5xx / network errors for a known progress URL are
  handled by REPOLL: the same remote Simulation is polled again, up to
  `repoll_attempts` times with growing backoff. No replacement POST is made;
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
from typing import Any

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

UNKNOWN_STATUSES = frozenset({"SUBMIT_UNKNOWN", "UNKNOWN"})


class Simulator:
    def __init__(self, client, max_concurrent=10, poll_timeout_sec=1500,
                 repoll_attempts=3, repoll_backoff_sec=60):
        self.client = client
        self.max_concurrent = max_concurrent
        self.poll_timeout_sec = poll_timeout_sec
        self.repoll_attempts = max(1, repoll_attempts)
        self.repoll_backoff_sec = max(0, repoll_backoff_sec)
        self.stop_dispatch = False
        self.paused_reason = None

    def run(self, executions, on_update=None):
        """Run transient execution records and return live remote outcomes."""
        self.stop_dispatch = False
        self.paused_reason = None
        pending = deque(executions)
        completed = []
        in_flight: dict[Any, Any] = {}

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
                    if exp.status in {"DONE", "FAILED"} and on_update is not None:
                        on_update(exp)
                    completed.append(exp)
                fill_window()

        if self.paused_reason:
            # 注意：不要用非 ASCII 前缀符号（如 U+23F8），Windows GBK 控制台
            # 会抛 UnicodeEncodeError 导致派发线程崩溃（基线测试已复现）。
            print(f"[PAUSED] 暂停新派发：{self.paused_reason}；先只读审计后继续。")
        return completed

    def run_multi(self, batches, on_update=None, max_concurrent=None):
        """Run bounded Multi-Simulation parent jobs.

        Each item in ``batches`` represents one parent POST and contains up
        to ten transient child execution records.  The parent is the guarded
        write identity; child results are attached only after BRAIN confirms
        their individual alpha ids.
        """
        self.stop_dispatch = False
        self.paused_reason = None
        pending = deque(batches)
        completed = []
        in_flight: dict[Any, Any] = {}
        workers = self.max_concurrent if max_concurrent is None else max_concurrent

        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="wqb-multi"
        ) as executor:
            def fill_window():
                while (
                    pending
                    and len(in_flight) < workers
                    and not self.stop_dispatch
                ):
                    batch = pending.popleft()
                    in_flight[executor.submit(
                        self._simulate_multi_one, batch, on_update
                    )] = batch

            fill_window()
            while in_flight:
                done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for future in done:
                    batch = in_flight.pop(future)
                    try:
                        future.result()
                    except Exception as exc:
                        batch.status = "UNKNOWN"
                        batch.error = f"UNKNOWN_LOCAL {type(exc).__name__}: {exc}"
                    if self._pauses_dispatch(batch):
                        self.stop_dispatch = True
                        self.paused_reason = f"{batch.status}:{batch.id}"
                    if batch.status in {"DONE", "FAILED"} and on_update is not None:
                        on_update(batch)
                    completed.append(batch)
                fill_window()

        if self.paused_reason:
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
        if exp.status == "RATE_LIMITED":
            return True
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
                for _submit_attempt in range(1, self.repoll_attempts + 1):
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
                        # The client has already received a server-side 429,
                        # which rejects the request before acceptance.  Do not
                        # retain a false unknown guard; stop this window so a
                        # later caller can retry after the rate limit clears.
                        experiment.error = f"{type(exc).__name__}: {exc}"
                        experiment.status = "RATE_LIMITED"
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
            for attempt in range(1, self.repoll_attempts + 1):
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
                    # The canonical result is the live BRAIN payload.  The
                    # simulator does not derive or persist local research
                    # metrics, yearly summaries, or eligibility decisions.
                    experiment.evidence = payload
                    experiment.status = "DONE"
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
                    if attempt < self.repoll_attempts:
                        delay = self.repoll_backoff_sec * attempt
                        print(
                            f"[REPOLL] {experiment.id} 第{attempt}/{self.repoll_attempts}"
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
            # 计时只用于当前调用的 transient result，不写入本地研究状态。
            experiment.elapsed_sec = time.time() - _t0

    def _simulate_multi_one(self, batch, on_update=None):
        """Submit and poll one Multi-Simulation parent exactly once."""
        _t0 = time.time()

        def persist():
            if on_update is not None and batch.status not in {"DONE", "FAILED"}:
                on_update(batch)

        try:
            if batch.status in ("SUBMITTING", "SUBMIT_UNKNOWN"):
                batch.status = "SUBMIT_UNKNOWN"
                batch.error = batch.error or (
                    "multi submission outcome unknown after interrupted POST"
                )
                persist()
                return batch

            if batch.progress_url:
                batch.status = "RUNNING"
                persist()
            else:
                batch.status = "SUBMITTING"
                persist()
                payloads = [
                    {
                        "type": "REGULAR",
                        "settings": dict(child.settings),
                        "regular": child.expression,
                    }
                    for child in batch.children
                ]
                try:
                    batch.progress_url = self.client.submit_multi_simulation(
                        payloads,
                        idempotency_key=batch.submission_fingerprint,
                    )
                except TypeError as exc:
                    if "idempotency_key" not in str(exc):
                        raise
                    batch.progress_url = self.client.submit_multi_simulation(payloads)
                batch.status = "RUNNING"
                persist()

            alpha_ids = self.client.poll_multi_progress(
                batch.progress_url, timeout_sec=self.poll_timeout_sec
            )
            if not isinstance(alpha_ids, (list, tuple)) or len(alpha_ids) != len(batch.children):
                raise WQBSimulationError(
                    "Multi-Simulation returned an incomplete child result set."
                )
            for child, alpha_id in zip(batch.children, alpha_ids):
                child.alpha_id = alpha_id
                child.evidence = self.client.get_alpha(alpha_id)
                child.status = "DONE"
            batch.status = "DONE"
            persist()
            return batch
        except WQBRejectedError as exc:
            batch.error = f"{type(exc).__name__}: {exc}"
            batch.status = "FAILED"
            for child in batch.children:
                child.status = "FAILED"
                child.error = batch.error
            persist()
            return batch
        except WQBAuthError as exc:
            batch.error = f"{type(exc).__name__}: {exc}"
            batch.status = "FAILED"
            for child in batch.children:
                child.status = "FAILED"
                child.error = batch.error
            persist()
            return batch
        except WQBSubmitUnknownError as exc:
            batch.error = f"{type(exc).__name__}: {exc}"
            batch.status = "SUBMIT_UNKNOWN"
            persist()
            return batch
        except WQBRateLimitError as exc:
            batch.error = f"{type(exc).__name__}: {exc}"
            batch.status = "RATE_LIMITED"
            persist()
            return batch
        except WQBError as exc:
            batch.error = f"{type(exc).__name__}: {exc}"
            batch.status = "UNKNOWN"
            persist()
            return batch
        except Exception as exc:
            batch.error = f"{type(exc).__name__}: {exc}"
            batch.status = "UNKNOWN"
            persist()
            return batch
        finally:
            batch.elapsed_sec = time.time() - _t0
