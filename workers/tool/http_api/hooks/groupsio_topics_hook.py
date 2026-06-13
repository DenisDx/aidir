"""Sample response hook for http_api worker.

This hook demonstrates the standardized signature used by response_hook_file.
"""


def transform_response(response: dict, context: dict) -> dict:
    """Normalize groups.io topics payload into a stable envelope fragment."""
    if not isinstance(response, dict):
        return {"items": []}

    payload = response.get("payload") if isinstance(response.get("payload"), dict) else {}
    topics = payload.get("topics") if isinstance(payload.get("topics"), list) else []
    next_token = payload.get("next_page_token")

    normalized_items = []
    for row in topics:
        if not isinstance(row, dict):
            continue
        normalized_items.append(
            {
                "id": row.get("id"),
                "title": row.get("subject") or row.get("title") or "",
            }
        )

    return {
        "items": normalized_items,
        "paging": {
            "next_page_token": next_token,
            "has_more": bool(next_token),
        },
        "meta": {
            "hook": "groupsio_topics_hook",
            "connector": context.get("connector") if isinstance(context, dict) else None,
            "operation": context.get("operation") if isinstance(context, dict) else None,
        },
    }
