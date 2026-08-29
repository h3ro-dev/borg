# /events pagination — read with an afterSeq loop, always

`GET /events?threadId=<id>&afterSeq=<n>` returns AT MOST 200 events per call
and carries no next-page marker. A reader that calls once with `afterSeq=0`
sees only the thread's first 200 events forever.

Field cost (2026-08-05, a production run): an unpaged reader made finished
threads look "still running" for hours — final answers sat on later pages —
and prompted one wrong turn/interrupt on a healthy, completed lane.

Correct pattern (from a workspace lane driver):

    items, after = [], 0
    while True:
        page = get(f"/events?threadId={tid}&afterSeq={after}")
        if not page: break
        items.extend(page)
        if page[-1].get("seq") is None or len(page) < 200: break
        after = page[-1]["seq"]

Consider adding a `nextAfterSeq`/`hasMore` field to the response, or a
`limit` parameter, so single-call readers fail loudly instead of silently.
