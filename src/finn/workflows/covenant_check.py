"""CovenantCheckWorkflow (D8, D9): one mail → one reply, waiting for input if needed; ID = the
Message-ID. Each round recomputes from the whole thread; deterministic reply Message-IDs make
`send_reply` idempotent. No I/O here: everything runs in activities."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, is_cancelled_exception

with workflow.unsafe.imports_passed_through():
    from finn import runs
    from finn.errors import Finding, Severity, stopping
    from finn.mail.thread import thread_id
    from finn.models.mail import InboundMessage, ParsedMessage, ReplyKind, SentRecord
    from finn.models.result import Report
    from finn.models.run import (
        CalcInput,
        ClassifyInput,
        FailureInput,
        ParseInput,
        PlanInput,
        RenderInput,
        RunOutcome,
        SendInput,
        StartRun,
        WorkflowState,
    )
    from finn.workflows.activities import NON_RETRYABLE, FinnActivities

_IO = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=3,
    non_retryable_error_types=NON_RETRYABLE,
)
_LLM = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=5,
    non_retryable_error_types=NON_RETRYABLE,
)
_ONCE = RetryPolicy(maximum_attempts=1)  # deterministic steps: a failure is an input error
_SEND = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2,
    maximum_interval=timedelta(minutes=10),
    maximum_attempts=12,  # SMTP outage: ~1 h of retries, Temporal keeps the state
    non_retryable_error_types=NON_RETRYABLE,
)


@workflow.defn
class CovenantCheckWorkflow:
    def __init__(self) -> None:
        self._inbox: list[InboundMessage] = []
        self._seen: list[str] = []
        self._waiting = False
        self._run_dir = ""
        self._seq = 0
        self._replies: list[str] = []
        self._sent: list[SentRecord] = []

    @workflow.signal
    def new_message(self, msg: InboundMessage) -> None:
        """A follow-up mail in the thread (already saved by the poller)."""
        if msg.message_id not in self._seen:
            self._seen.append(msg.message_id)
            self._inbox.append(msg)

    @workflow.query
    def state(self) -> WorkflowState:
        return WorkflowState(
            waiting_for_input=self._waiting, run_dir=self._run_dir, seen_message_ids=self._seen
        )

    @workflow.run
    async def run(self, start: StartRun) -> RunOutcome:
        self._run_dir = start.run_dir
        self._seen.append(start.inbound.message_id)
        wid = workflow.info().workflow_id
        session = thread_id(start.inbound)
        latest = start.inbound
        try:
            thread = [await self._parse(start.inbound)]
            while True:
                classified = await workflow.execute_activity_method(
                    FinnActivities.classify_intent,
                    ClassifyInput(
                        thread=thread, run_dir=self._run_dir, workflow_id=wid, session_id=session
                    ),
                    start_to_close_timeout=timedelta(minutes=15),
                    retry_policy=_LLM,
                )
                req = classified.request
                if req.intent in ("other", "question_about_result"):
                    await self._reply("other", latest, wid, session, question=req.intent != "other")
                    return self._outcome("replied")

                stops: list[Finding] = stopping(classified.input.findings)
                plan_file = classified.input.plan
                kind: ReplyKind = "ask"
                if classified.input.status == "ok" and plan_file is not None:
                    plan_result = await workflow.execute_activity_method(
                        FinnActivities.load_and_validate_plan,
                        PlanInput(attachment=plan_file, request=req, run_dir=self._run_dir),
                        start_to_close_timeout=timedelta(minutes=2),
                        retry_policy=_ONCE,
                    )
                    stops = stopping(plan_result.findings)
                    if not stops:
                        report: Report = await workflow.execute_activity_method(
                            FinnActivities.calculate,
                            CalcInput(
                                plan_result=plan_result,
                                request=req,
                                file=plan_file.filename,
                                run_dir=self._run_dir,
                            ),
                            start_to_close_timeout=timedelta(minutes=2),
                            retry_policy=_ONCE,
                        )
                        await self._reply(
                            "result", latest, wid, session, report=report,
                            findings=classified.input.findings,
                        )  # fmt: skip
                        return self._outcome("replied")
                    if any(f.severity == Severity.BLOCK for f in stops):
                        kind = "block"

                # Thread case B: ask once per round, then wait for a follow-up in the thread.
                await self._reply(
                    kind, latest, wid, session, findings=stops,
                    file=plan_file.filename if plan_file else None,
                )  # fmt: skip
                self._waiting = True
                try:
                    await workflow.wait_condition(
                        lambda: bool(self._inbox),
                        timeout=timedelta(days=start.wait_for_input_days),
                    )
                except TimeoutError:
                    workflow.logger.info("no follow-up within the waiting time (WAIT_TIMEOUT)")
                    return self._outcome("wait_timeout")
                finally:
                    self._waiting = False
                while self._inbox:
                    latest = self._inbox.pop(0)
                    thread.append(await self._parse(latest))
        except ActivityError as exc:
            if is_cancelled_exception(exc):
                raise
            await self._fail(exc, latest, wid, session)
            raise ApplicationError(f"run failed: {exc.cause or exc}") from exc

    async def _parse(self, msg: InboundMessage) -> ParsedMessage:
        return await workflow.execute_activity_method(
            FinnActivities.parse_message,
            ParseInput(inbound=msg, run_dir=self._run_dir),
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=_IO,
        )

    async def _reply(
        self,
        kind: ReplyKind,
        reply_to: InboundMessage,
        wid: str,
        session: str,
        *,
        report: Report | None = None,
        findings: list[Finding] | None = None,
        file: str | None = None,
        question: bool = False,
    ) -> None:
        self._seq += 1
        draft = await workflow.execute_activity_method(
            FinnActivities.render_reply,
            RenderInput(
                kind=kind,
                reply_to=reply_to,
                run_dir=self._run_dir,
                workflow_id=wid,
                session_id=session,
                seq=self._seq,
                report=report,
                findings=findings or [],
                file=file,
                question=question,
            ),
            start_to_close_timeout=timedelta(minutes=15),
            retry_policy=_LLM,
        )
        sent = await workflow.execute_activity_method(
            FinnActivities.send_reply,
            SendInput(draft=draft, run_dir=self._run_dir),
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=_SEND,
        )
        self._replies.append(kind)
        self._sent.append(sent)

    async def _fail(
        self, exc: ActivityError, latest: InboundMessage, wid: str, session: str
    ) -> None:
        """INFRA_UNEXPECTED: error.txt for the operator, a polite mail with the reference."""
        detail = f"{type(exc.cause).__name__ if exc.cause else 'ActivityError'}: {exc.cause or exc}"
        try:
            await workflow.execute_activity_method(
                FinnActivities.record_failure,
                FailureInput(run_dir=self._run_dir, error=f"{exc.activity_type}: {detail}"),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_IO,
            )
            if "send" not in (exc.activity_type or ""):  # mail itself broken: don't retry it
                await self._reply("error", latest, wid, session)
        except ActivityError:
            workflow.logger.exception("error reply failed")

    def _outcome(self, status: str) -> RunOutcome:
        return RunOutcome.model_validate(
            {
                "status": status,
                "replies": self._replies,
                "sent": self._sent,
                "run_id": runs.run_id(self._run_dir),
            }
        )
