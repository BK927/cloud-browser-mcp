"""Navigation evidence, independent of URL equality or browser engine objects."""


def outcome(before, after, *, operation=None):
    url_changed = before["url"] != after["url"]
    document_changed = before["document"] != after["document"]
    same_document = not document_changed and (
        url_changed or before["sequence"] != after["sequence"]
    )
    kind = (
        "reload"
        if document_changed and operation == "reload"
        else "full_document"
        if document_changed
        else "same_document"
        if same_document
        else "none"
    )
    return {
        "navigation_occurred": kind != "none",
        "url_changed": url_changed,
        "document_changed": document_changed,
        "navigation_kind": kind,
        # Never infer History API vs fragment navigation from URL shape alone.
        "same_document_kind": after.get("same_document_kind")
        if same_document and before["sequence"] != after["sequence"]
        else None,
    }
