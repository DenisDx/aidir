"""Minimal hook example."""

ENABLED = False


async def on_task_created(task):
    """Observe a newly created task."""
    # task.priority = 100
    # task.hook_metadata["example"] = "created"
    # await hooks.cancel_task(task, reason="Rejected by example hook")


async def before_llm_request(task, request):
    """Observe a task before inference."""
    # task.model = "another-model"
    # request["temperature"] = 0
    # await hooks.cancel_task(task, reason="Inference disabled by example hook")


def register(hooks):
    """Register example handlers when enabled."""
    if not ENABLED:
        return

    hooks.on("task_created", on_task_created)
    hooks.on("before_llm_request", before_llm_request)
