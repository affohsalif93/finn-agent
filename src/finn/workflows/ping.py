"""PingWorkflow: smoke test for the stack (`finn ping`)."""

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from finn.workflows.activities import ping


@workflow.defn
class PingWorkflow:
    @workflow.run
    async def run(self) -> str:
        return await workflow.execute_activity(ping, start_to_close_timeout=timedelta(seconds=10))
