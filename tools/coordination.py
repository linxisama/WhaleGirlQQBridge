"""File-based handoff interface for GPT, DeepSeek and read-only WorkBuddy.

The role argument records accountability; it is not an OS security boundary.
Only reviewed proposals may modify source through this interface.
"""
import argparse
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROLES = ("gpt", "deepseek", "workbuddy")


class Coordination:
    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()
        (self.root / "collaboration").mkdir(parents=True, exist_ok=True)
        for role in ROLES:
            for folder in ("tasks", "inbox", "outbox", "reports"):
                (self.root / "agents" / role / folder).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "collaboration/state.sqlite3", timeout=30)
        self.db.execute("CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self.sync()

    def close(self):
        self.db.close()

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()

    def path(self, relative):
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise ValueError("路径必须位于 QQ 项目内")
        return path

    def get(self, task_id):
        row = self.db.execute("SELECT body FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise ValueError("任务不存在")
        return json.loads(row[0])

    def _event(self, actor, task, action, message, artifact=""):
        if actor not in ROLES:
            raise ValueError("未知角色")
        event = {"id": uuid.uuid4().hex, "time": self.now(), "actor": actor,
                 "recipients": [r for r in ROLES if r != actor], "task_id": task,
                 "action": action, "message": message, "artifact": artifact}
        self.db.execute("INSERT INTO events VALUES(?,?)", (event["id"], json.dumps(event, ensure_ascii=False)))

    def _write_task(self, task):
        self.db.execute("INSERT OR REPLACE INTO tasks VALUES(?,?)", (task["id"], json.dumps(task, ensure_ascii=False)))

    def create(self, title, owner, scope, acceptance, depends_on=None):
        if owner not in ROLES:
            raise ValueError("未知负责人")
        for name in scope:
            self.path(name)
        task = {"id": "TASK-" + uuid.uuid4().hex[:10], "title": title, "owner": owner,
                "scope": scope, "acceptance": acceptance, "depends_on": depends_on or [],
                "status": "assigned", "created": self.now(), "updates": []}
        with self.db:
            self._write_task(task)
            self._event("gpt", task["id"], "assigned", title)
        self.sync()
        return task

    def update(self, actor, task_id, state, message, artifact=""):
        if state not in {"in_progress", "submitted", "blocked", "changes_requested", "complete"}:
            raise ValueError("未知状态")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            task = self.get(task_id)
            if actor != task["owner"] and actor != "gpt":
                raise ValueError("只能更新自己的任务")
            if state in {"complete", "changes_requested"} and actor != "gpt":
                raise ValueError("只有 GPT 可以验收或要求返工")
            if artifact:
                path = self.path(artifact)
                if not path.is_file() or not path.is_relative_to(self.root / "agents" / actor):
                    raise ValueError("交付物必须位于当前角色目录")
            task["status"] = state
            task["updates"].append({"actor": actor, "time": self.now(), "state": state, "message": message, "artifact": artifact})
            self._write_task(task)
            self._event(actor, task_id, state, message, artifact)
        self.sync()
        return task

    def notify(self, actor, task_id, message, artifact=""):
        self.get(task_id)
        if artifact and not self.path(artifact).is_file():
            raise ValueError("交付文件不存在")
        with self.db:
            self._event(actor, task_id, "progress", message, artifact)
        self.sync()

    def sync(self):
        # SQLite is authoritative; interrupted file delivery is safely replayed.
        for (body,) in self.db.execute("SELECT body FROM events ORDER BY rowid"):
            event = json.loads(body)
            for role in event["recipients"]:
                path = self.root / "agents" / role / "inbox" / (event["id"] + ".json")
                if not path.exists():
                    self._atomic(path, body)
            path = self.root / "agents" / event["actor"] / "outbox" / (event["id"] + ".json")
            if not path.exists():
                self._atomic(path, body)
        for (body,) in self.db.execute("SELECT body FROM tasks ORDER BY rowid"):
            task = json.loads(body)
            self._atomic(self.root / "agents" / task["owner"] / "tasks" / (task["id"] + ".json"), body)

    @staticmethod
    def _atomic(path, text):
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)

    def review_workbuddy(self, task_id, report, decisions):
        task = self.get(task_id)
        if task["owner"] != "workbuddy":
            raise ValueError("必须指定 WorkBuddy 检查任务")
        report_path = self.path(report)
        if not report_path.is_relative_to(self.root / "agents/workbuddy/reports"):
            raise ValueError("必须读取 WorkBuddy 报告目录")
        findings = json.loads(report_path.read_text(encoding="utf-8"))["findings"]
        decision_path = self.path(decisions)
        if not decision_path.is_relative_to(self.root / "agents/gpt/reports"):
            raise ValueError("GPT 复核结论必须保存在自己的报告目录")
        reviews = json.loads(decision_path.read_text(encoding="utf-8"))["decisions"]
        if {f["id"] for f in findings} != {d["finding_id"] for d in reviews}:
            raise ValueError("GPT 必须逐条复核，不能遗漏或增加检查项")
        for item in reviews:
            if item.get("decision") not in {"accept", "reject", "needs_evidence"} or not item.get("evidence"):
                raise ValueError("每项必须给出采纳/驳回/待证实及复现证据")
        self.notify("gpt", task_id, "已逐条复核 WorkBuddy；只有 accept 项可以派给 DeepSeek", decisions)
        return [d for d in reviews if d["decision"] == "accept"]

    def approve_proposal(self, task_id, proposal, review):
        task = self.get(task_id)
        path, report = self.path(proposal), self.path(review)
        if task["owner"] != "deepseek" or task["status"] != "submitted":
            raise ValueError("仅可审批 DeepSeek 已提交的任务")
        if not path.is_relative_to(self.root / "agents/deepseek/reports") or not report.is_relative_to(self.root / "agents/gpt/reports") or not report.is_file():
            raise ValueError("提案和 GPT 审核文件目录不正确")
        approval = {"task_id": task_id, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "review": review, "approved_by": "gpt", "time": self.now()}
        approval_path = self.root / "agents/gpt/reports" / (task_id + "-approval.json")
        self._atomic(approval_path, json.dumps(approval, ensure_ascii=False, indent=2))
        self.notify("gpt", task_id, "GPT 已审核并批准此版本提案应用，尚待测试验收", str(approval_path.relative_to(self.root)))

    def apply_proposal(self, task_id, proposal):
        task = self.get(task_id)
        path = self.path(proposal)
        approval = json.loads((self.root / "agents/gpt/reports" / (task_id + "-approval.json")).read_text(encoding="utf-8"))
        if task["owner"] != "deepseek" or task["status"] != "submitted" or approval["sha256"] != hashlib.sha256(path.read_bytes()).hexdigest():
            raise ValueError("提案未获 GPT 批准或批准后发生了修改")
        proposal_data = json.loads(path.read_text(encoding="utf-8"))
        changes = proposal_data["changes"]
        prepared = {}
        for change in changes:
            relative = change["path"]
            if relative not in task["scope"]:
                raise ValueError("提案修改超出任务范围")
            target = self.path(relative)
            text = prepared.get(relative, target.read_text(encoding="utf-8") if target.exists() else "")
            expected = proposal_data["base_sha256"].get(relative)
            original = target.read_bytes() if target.exists() else b""
            if hashlib.sha256(original).hexdigest() != expected:
                raise ValueError("源文件已变化，需要重新生成/复核提案")
            old, new = change["old"], change["new"]
            if old:
                if text.count(old) != 1:
                    raise ValueError("替换位置不唯一")
                text = text.replace(old, new, 1)
            elif text:
                raise ValueError("新文件操作不能覆盖已有文件")
            else:
                text = new
            prepared[relative] = text
        # Validate the whole proposal before touching source; save per-task backups.
        backup = self.root / "agents/deepseek/reports" / (task_id + "-backup")
        for relative, text in prepared.items():
            target = self.path(relative)
            saved = backup / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                saved.write_bytes(target.read_bytes())
            target.parent.mkdir(parents=True, exist_ok=True)
            self._atomic(target, text)
        self.notify("deepseek", task_id, "已应用 GPT 批准的实现，等待验证", proposal)


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--owner", choices=ROLES, required=True)
    create.add_argument("--title", required=True)
    create.add_argument("--scope", nargs="*", default=[])
    create.add_argument("--acceptance", required=True)
    update = commands.add_parser("update")
    update.add_argument("--actor", choices=ROLES, required=True)
    update.add_argument("--task", required=True)
    update.add_argument("--state", required=True)
    update.add_argument("--message", required=True)
    update.add_argument("--artifact", default="")
    for command in ("approve", "apply"):
        sub = commands.add_parser(command)
        sub.add_argument("--task", required=True)
        sub.add_argument("--proposal", required=True)
        if command == "approve":
            sub.add_argument("--review", required=True)
    review = commands.add_parser("review-workbuddy")
    review.add_argument("--task", required=True)
    review.add_argument("--report", required=True)
    review.add_argument("--decisions", required=True)
    commands.add_parser("status")
    commands.add_parser("sync")
    args = parser.parse_args()
    bus = Coordination()
    try:
        if args.command == "create":
            result = bus.create(args.title, args.owner, args.scope, args.acceptance)
        elif args.command == "update":
            result = bus.update(args.actor, args.task, args.state, args.message, args.artifact)
        elif args.command == "approve":
            result = bus.approve_proposal(args.task, args.proposal, args.review)
        elif args.command == "apply":
            result = bus.apply_proposal(args.task, args.proposal)
        elif args.command == "review-workbuddy":
            result = bus.review_workbuddy(args.task, args.report, args.decisions)
        elif args.command == "status":
            result = [json.loads(r[0]) for r in bus.db.execute("SELECT body FROM tasks ORDER BY rowid")]
        else:
            bus.sync()
            result = {"synced": True}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        bus.close()


if __name__ == "__main__":
    main()
