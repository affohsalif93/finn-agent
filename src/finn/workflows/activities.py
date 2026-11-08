"""Activities (ARCHITECTURE §7.3): thin wrappers, each calls one `pipeline` step.

Sync activities (file I/O, openpyxl, imaplib, the OpenAI SDK) run in the worker's thread pool.
Dependencies are injected (`FinnActivities(settings, connector_factory)`) so tests use a fake
mailbox. Input errors (validation) are non-retryable application errors; everything else follows
the retry policy the workflow sets per activity.
"""

from collections.abc import Callable

from pydantic import ValidationError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from finn import pipeline
from finn.config import Settings
from finn.mail.connector import MailAuthError, MailConnector
from finn.models.mail import Classified, ParsedMessage, ReplyDraft, SentRecord
from finn.models.result import Report
from finn.models.run import (
    CalcInput,
    ClassifyInput,
    FailureInput,
    ParseInput,
    PlanInput,
    RenderInput,
    SendInput,
)
from finn.plan.reader import PlanReadResult

NON_RETRYABLE = ["InputError", "MailAuthError"]


def _input_errors[T](fn: Callable[[], T]) -> T:
    try:
        return fn()
    except (ValidationError, ValueError, FileNotFoundError) as exc:
        raise ApplicationError(
            f"{type(exc).__name__}: {exc}", type="InputError", non_retryable=True
        ) from exc


def default_connector_factory(settings: Settings) -> Callable[[], MailConnector]:
    def make() -> MailConnector:
        from finn.mail.imap_smtp import ImapSmtpConnector

        return ImapSmtpConnector(settings)

    return make


class FinnActivities:
    def __init__(
        self, settings: Settings, connector_factory: Callable[[], MailConnector] | None = None
    ) -> None:
        self.settings = settings
        self.connector_factory = connector_factory or default_connector_factory(settings)

    @activity.defn(name="parse_message")
    def parse_message(self, inp: ParseInput) -> ParsedMessage:
        return _input_errors(lambda: pipeline.parse_message(inp, self.settings))

    @activity.defn(name="classify_intent")
    def classify_intent(self, inp: ClassifyInput) -> Classified:
        return pipeline.classify_intent(inp, self.settings)

    @activity.defn(name="load_and_validate_plan")
    def load_and_validate_plan(self, inp: PlanInput) -> PlanReadResult:
        return _input_errors(lambda: pipeline.load_and_validate_plan(inp))

    @activity.defn(name="calculate")
    def calculate(self, inp: CalcInput) -> Report:
        return pipeline.calculate(inp, self.settings)

    @activity.defn(name="render_reply")
    def render_reply(self, inp: RenderInput) -> ReplyDraft:
        return pipeline.render_reply(inp, self.settings)

    @activity.defn(name="send_reply")
    def send_reply(self, inp: SendInput) -> SentRecord:
        connector = self.connector_factory()
        try:
            return pipeline.send_reply(inp, connector)
        except MailAuthError as exc:  # loud, no retry loop (INFRA_MAILBOX_AUTH)
            activity.logger.error("smtp/imap login rejected: %s", exc)
            raise ApplicationError(str(exc), type="MailAuthError", non_retryable=True) from exc
        finally:
            close = getattr(connector, "close", None)
            if callable(close):
                close()

    @activity.defn(name="record_failure")
    def record_failure(self, inp: FailureInput) -> None:
        pipeline.record_failure(inp)

    def all(self) -> list[Callable[..., object]]:
        return [
            self.parse_message,
            self.classify_intent,
            self.load_and_validate_plan,
            self.calculate,
            self.render_reply,
            self.send_reply,
            self.record_failure,
        ]
