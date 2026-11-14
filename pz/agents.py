"""Agent brains.

Every brain acts only through a recorder Session (retrieve / tool_call / memory_write),
so the same code path serves live runs and counterfactual replays.

RuleBrain is deterministic and needs no API key: it behaves like an LLM agent that
follows instructions found in its context. It also knows when it used the poisoned
rule, which gives us ground truth for scoring.
"""
from .scenarios import WORM_RE, fmt_amount, rule_regex


def find_rule(sc, ctx):
    rx = rule_regex(sc)
    for r in ctx:
        m = rx.search(r["text"])
        if m:
            return int(m.group(1)), m.group(2), r
    return None


class RuleBrain:
    def __init__(self, scenario):
        self.sc = scenario
        self.t = scenario["topics"]

    def run(self, sess, task):
        getattr(self, task["kind"])(sess, task)

    def _ctx(self, sess, category):
        return sess.retrieve([self.t["base"], f"{self.t['base']}:{category}"])

    # primary agent: creates items (refund / payment / PR / quote)
    def create_item(self, sess, t):
        ctx = self._ctx(sess, t["category"])
        value = sess.s.table_as_of("entities", sess.ts)[t["entity_id"]]["value"]
        rule = find_rule(self.sc, ctx)
        used = bool(rule and t["amount"] > rule[0])
        if used:
            value = rule[1]
        sess.tool_call("create_item", "items", t["item_id"],
                       {"entity_id": t["entity_id"], "category": t["category"], "amount": t["amount"], "value": value, "status": "open"},
                       poisoned=used)
        # the worm: the injected text asks the agent to persist it for other teams
        if rule and WORM_RE.search(rule[2]["text"]) and not any(rule[1] in r["text"] for r in sess.retrieve(self.t["review"])):
            sess.memory_write(f"mem_{sess.id}", self.t["review"], self.sc["memory_text"].format(thr=rule[0], value=rule[1]), poisoned=True)

    # reviewer agent: batch review, may change the value, then approves
    def review_batch(self, sess, t):
        rule = find_rule(self.sc, sess.retrieve(self.t["review"]))
        for iid, r in sorted(sess.s.table_as_of("items", sess.ts).items()):
            if r["status"] != "open":
                continue
            if rule and r["amount"] > rule[0] and r["value"] != rule[1]:
                sess.tool_call("change_value", "items", iid, {"value": rule[1]}, poisoned=True)
            sess.tool_call("approve", "items", iid, {"status": "approved"}, poisoned=False)

    # notifier agent: irreversible messages quoting the value
    def notify(self, sess, t):
        ctx = self._ctx(sess, t["category"])
        item = sess.s.table_as_of("items", sess.ts).get(t["item_id"])
        if not item:
            return
        rule = find_rule(self.sc, ctx)
        used = bool(rule and item["amount"] > rule[0])
        tpl = self.sc["texts"]["notify_bad" if used else "notify_ok"]
        body = tpl.format(row=t["item_id"], amount=fmt_amount(self.sc, item["amount"]), value=rule[1] if used else item["value"])
        sess.tool_call("send_message", "messages", f"msg_{sess.id}",
                       {"to_addr": t["email"], "subject": f"About {t['item_id']}", "body": body},
                       reversible=False, poisoned=used)

    def upgrade(self, sess, t):
        sess.retrieve(self.t["other"])
        sess.tool_call("upgrade", "entities", t["entity_id"], {"tier": t["tier"]}, poisoned=False)

    # nightly job (not an LLM): finalizes approved items
    def finalize(self, sess, t):
        for iid, r in sorted(sess.s.table_as_of("items", sess.ts).items()):
            if r["status"] == "approved":
                sess.tool_call("finalize", "items", iid, {"status": "final"}, poisoned=False)
