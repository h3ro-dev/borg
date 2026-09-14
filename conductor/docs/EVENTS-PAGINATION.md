# /events pagination — read with an afterSeq loop, always

`GET /events?threadId=<id>&afterSeq=<n>&limit=<n>` returns at most 200 events
per call. It returns `nextAfterSeq` and `hasMore`; a requested limit above 200
is capped. A reader that calls once with `afterSeq=0` still sees only the
thread's first page.

Correct reader pattern:

    items, after = [], 0
    while True:
        response = get(f"/events?threadId={tid}&afterSeq={after}")
        items.extend(response["events"])
        if not response["hasMore"]: break
        after = response["nextAfterSeq"]

The response shape is `{events,lastSeq,nextAfterSeq,hasMore}`. `lastSeq` is the
current ring cursor; `nextAfterSeq` is the exact cursor for the next request.
