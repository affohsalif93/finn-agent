"""Activities. Only a placeholder until the pipeline steps exist."""

from temporalio import activity


@activity.defn
def ping() -> str:
    """Trivial activity used to verify worker <-> Temporal end to end."""
    activity.logger.info("ping activity called")
    return "pong"
