#!/usr/bin/env python3
"""quota_sim: 配额树继承与借用模拟器（仅 Python 标准库）。

用法:
    python3 quota_sim.py --trees <树目录> --script <脚本文件> [--html <输出路径>]

文本结果写 stdout, 诊断写 stderr; 输入非法时退出码 2, 正常结束退出码 0。
"""

import argparse
import json
import os
import re
import sys
from collections import deque

MAX_QUOTA = 10 ** 9
MAX_TIME = 10 ** 18
ID_RE = re.compile(r"[A-Za-z0-9_.-]+")
UINT_RE = re.compile(r"[0-9]+")


class InputError(Exception):
    """输入非法; line 为 1 起始的物理行号, 无法定位时为 0。"""

    def __init__(self, line, message):
        super().__init__(message)
        self.line = line
        self.message = message


def fail(line, message):
    raise InputError(line, message)


def parse_uint(text, line, what, lo, hi):
    if not UINT_RE.fullmatch(text):
        fail(line, "%s 必须是非负整数: %r" % (what, text))
    value = int(text)
    if not lo <= value <= hi:
        fail(line, "%s 超出范围 [%d, %d]: %d" % (what, lo, hi, value))
    return value


def effective_lines(path):
    """产出 (行号, 内容); 跳过空行与 # 注释行。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = list(fh)
    except OSError as exc:
        fail(0, "无法读取文件 %s: %s" % (path, exc))
    except UnicodeDecodeError as exc:
        fail(0, "文件 %s 不是合法 UTF-8: %s" % (path, exc))
    for lineno, raw in enumerate(lines, 1):
        line = raw.strip()
        if line and not line.startswith("#"):
            yield lineno, line


class Node:
    __slots__ = ("id", "parent", "children", "quota", "own", "debt", "lent")

    def __init__(self, nid, parent, quota, used):
        self.id = nid
        self.parent = parent
        self.children = []
        self.quota = quota
        self.own = used
        self.debt = 0
        self.lent = 0

    @property
    def used(self):
        return self.own + self.debt

    @property
    def idle(self):
        return self.quota - self.own - self.lent


class Record:
    """一条借用台账: 记录号 seq (b<seq>), 余额清零即移出。"""

    __slots__ = ("seq", "borrower", "lender", "amount", "remain", "start")

    def __init__(self, seq, borrower, lender, amount, start):
        self.seq = seq
        self.borrower = borrower
        self.lender = lender
        self.amount = amount
        self.remain = amount
        self.start = start


class Op:
    __slots__ = ("lineno", "oid", "t", "action", "node", "amount")

    def __init__(self, lineno, oid, t, action, node, amount):
        self.lineno = lineno
        self.oid = oid
        self.t = t
        self.action = action
        self.node = node
        self.amount = amount


def load_tree(path):
    nodes = []
    by_id = {}
    root = None
    for lineno, line in effective_lines(path):
        fields = line.split()
        if len(fields) != 5 or fields[0] != "node":
            fail(lineno, "树文件行格式应为: node <id> <parent> <quota> <used>")
        _, nid, parent_id, quota_s, used_s = fields
        if not ID_RE.fullmatch(nid):
            fail(lineno, "非法节点 id: %r" % nid)
        if nid in by_id:
            fail(lineno, "重复节点 id: %s" % nid)
        quota = parse_uint(quota_s, lineno, "quota", 0, MAX_QUOTA)
        used = parse_uint(used_s, lineno, "used", 0, MAX_QUOTA)
        if used > quota:
            fail(lineno, "used 超过 quota: %d > %d" % (used, quota))
        if parent_id == "-":
            if root is not None or nodes:
                fail(lineno, "根节点必须唯一且是第一条有效行")
            node = Node(nid, None, quota, used)
            root = node
        else:
            if root is None:
                fail(lineno, "第一条有效行必须是根节点")
            parent = by_id.get(parent_id)
            if parent is None:
                fail(lineno, "未知父节点: %s" % parent_id)
            node = Node(nid, parent, quota, used)
            parent.children.append(node)
        nodes.append(node)
        by_id[nid] = node
    if root is None:
        fail(0, "树文件为空: %s" % path)
    return nodes, by_id, root


def load_script(path):
    tree_name = None
    tree_lineno = 0
    timeout = None
    ops = []
    op_ids = set()
    prev_t = None
    stage = 0
    for lineno, line in effective_lines(path):
        fields = line.split()
        if stage == 0:
            if len(fields) != 2 or fields[0] != "tree":
                fail(lineno, "脚本第一条有效行应为: tree <名字>")
            tree_name = fields[1]
            if not ID_RE.fullmatch(tree_name) or tree_name in (".", ".."):
                fail(lineno, "非法树名: %r" % tree_name)
            tree_lineno = lineno
            stage = 1
        elif stage == 1:
            if len(fields) != 2 or fields[0] != "timeout":
                fail(lineno, "脚本第二条有效行应为: timeout <T>")
            timeout = parse_uint(fields[1], lineno, "timeout", 0, MAX_TIME)
            stage = 2
        else:
            if len(fields) != 5:
                fail(lineno, "操作行格式: <操作号> t=<t> <APPLY|RELEASE> <节点> <额度>")
            oid, tfield, action, nid, amount_s = fields
            if oid in op_ids:
                fail(lineno, "重复操作号: %s" % oid)
            op_ids.add(oid)
            if not tfield.startswith("t="):
                fail(lineno, "时间字段应为 t=<非负整数>: %r" % tfield)
            t = parse_uint(tfield[2:], lineno, "t", 0, MAX_TIME)
            if prev_t is not None and t < prev_t:
                fail(lineno, "t 必须单调不减: %d < %d" % (t, prev_t))
            prev_t = t
            if action not in ("APPLY", "RELEASE"):
                fail(lineno, "未知动作: %s" % action)
            amount = parse_uint(amount_s, lineno, "额度", 1, MAX_QUOTA)
            ops.append(Op(lineno, oid, t, action, nid, amount))
    if stage < 2:
        fail(0, "脚本缺少 tree/timeout 头: %s" % path)
    return tree_name, tree_lineno, timeout, ops


class Engine:
    """配额树状态机: 每步先结算超时, 再执行操作。"""

    def __init__(self, nodes, timeout):
        self.nodes = nodes
        self.timeout = timeout
        self.records = {}          # seq -> Record (未还)
        self.by_borrower = {}      # borrower id -> [Record], 按记录号升序
        self.expiry = deque()      # (start, seq), start 单调不减
        self.seq = 0

    def free(self, node):
        total = 0
        cur = node
        while cur is not None:
            total += cur.idle
            cur = cur.parent
        return total

    def _drop(self, rec):
        del self.records[rec.seq]
        lst = self.by_borrower[rec.borrower.id]
        lst.remove(rec)
        if not lst:
            del self.by_borrower[rec.borrower.id]

    def settle_timeout(self, t):
        """强制收回所有 t - start >= T 的未还记录, 返回 EVENT 列表。"""
        limit = t - self.timeout
        batch = []
        while self.expiry and self.expiry[0][0] <= limit:
            _, seq = self.expiry.popleft()
            rec = self.records.get(seq)
            if rec is not None:
                batch.append(rec)
        batch.sort(key=lambda r: (r.start, r.borrower.id, r.seq))
        events = []
        for rec in batch:
            amount = rec.remain
            rec.borrower.debt -= amount
            rec.lender.lent -= amount
            self._drop(rec)
            events.append((rec, amount))
        return events

    def apply(self, node, amount, t):
        """成功返回本次借入总量; 拒绝返回 None (零状态变化)。"""
        if self.free(node) < amount:
            return None
        take = min(node.idle, amount)
        node.own += take
        remaining = amount - take
        borrowed = 0
        anc = node.parent
        while remaining > 0 and anc is not None:
            idle = anc.idle
            if idle > 0:
                take = min(idle, remaining)
                anc.lent += take
                node.debt += take
                self.seq += 1
                rec = Record(self.seq, node, anc, take, t)
                self.records[rec.seq] = rec
                self.by_borrower.setdefault(node.id, []).append(rec)
                self.expiry.append((t, rec.seq))
                borrowed += take
                remaining -= take
            anc = anc.parent
        return borrowed

    def release(self, node, amount):
        """成功返回本次偿还总量; 拒绝返回 None (零状态变化)。"""
        if amount > node.used:
            return None
        left = amount
        repaid = 0
        for rec in list(self.by_borrower.get(node.id, [])):
            if left == 0:
                break
            pay = min(rec.remain, left)
            rec.remain -= pay
            rec.lender.lent -= pay
            node.debt -= pay
            repaid += pay
            left -= pay
            if rec.remain == 0:
                self._drop(rec)
        node.own -= left
        return repaid


def simulate(nodes, by_id, timeout, ops, keep_steps=False):
    """返回 (文本行列表, 每步快照列表); keep_steps=False 时快照为 None。"""
    engine = Engine(nodes, timeout)
    lines = []
    steps = []
    for op in ops:
        events = engine.settle_timeout(op.t)
        event_dicts = []
        for rec, reclaimed in events:
            lines.append(
                "EVENT t=%d action=RECLAIM id=b%d borrower=%s lender=%s "
                "amount=%d start=%d"
                % (op.t, rec.seq, rec.borrower.id, rec.lender.id,
                   reclaimed, rec.start)
            )
            event_dicts.append({
                "id": "b%d" % rec.seq,
                "borrower": rec.borrower.id,
                "lender": rec.lender.id,
                "amount": reclaimed,
                "start": rec.start,
            })
        node = by_id[op.node]
        if op.action == "APPLY":
            borrowed = engine.apply(node, op.amount, op.t)
            if borrowed is None:
                available = engine.free(node)
                result = "REJECT"
                detail = "INSUFFICIENT available=%d" % available
                lines.append(
                    "STEP t=%d id=%s action=APPLY node=%s amount=%d "
                    "result=REJECT reason=INSUFFICIENT available=%d"
                    % (op.t, op.oid, node.id, op.amount, available)
                )
            else:
                result = "OK"
                detail = "borrowed=%d" % borrowed
                lines.append(
                    "STEP t=%d id=%s action=APPLY node=%s amount=%d "
                    "result=OK borrowed=%d"
                    % (op.t, op.oid, node.id, op.amount, borrowed)
                )
        else:
            repaid = engine.release(node, op.amount)
            if repaid is None:
                result = "REJECT"
                detail = "UNDERFLOW used=%d" % node.used
                lines.append(
                    "STEP t=%d id=%s action=RELEASE node=%s amount=%d "
                    "result=REJECT reason=UNDERFLOW used=%d"
                    % (op.t, op.oid, node.id, op.amount, node.used)
                )
            else:
                result = "OK"
                detail = "repaid=%d" % repaid
                lines.append(
                    "STEP t=%d id=%s action=RELEASE node=%s amount=%d "
                    "result=OK repaid=%d"
                    % (op.t, op.oid, node.id, op.amount, repaid)
                )
        parts = []
        for n in engine.nodes:
            parts.append("%s=%d/%d/%d/%d" % (n.id, n.used, n.quota, n.debt, n.lent))
        lines.append("STATE t=%d %s" % (op.t, " ".join(parts)))
        if engine.records:
            debt_parts = []
            for seq in sorted(engine.records):
                rec = engine.records[seq]
                debt_parts.append(
                    "b%d=%s->%s:%d/%d@%d"
                    % (rec.seq, rec.borrower.id, rec.lender.id,
                       rec.remain, rec.amount, rec.start)
                )
            lines.append("DEBT t=%d %s" % (op.t, " ".join(debt_parts)))
        else:
            lines.append("DEBT t=%d -" % op.t)
        if keep_steps:
            state = {n.id: [n.used, n.own, n.debt, n.lent] for n in engine.nodes}
            debts = [{
                "id": "b%d" % rec.seq,
                "borrower": rec.borrower.id,
                "lender": rec.lender.id,
                "remain": rec.remain,
                "amount": rec.amount,
                "start": rec.start,
            } for rec in (engine.records[s] for s in sorted(engine.records))]
            steps.append({
                "t": op.t,
                "id": op.oid,
                "action": op.action,
                "node": node.id,
                "amount": op.amount,
                "result": result,
                "detail": detail,
                "events": event_dicts,
                "state": state,
                "debts": debts,
            })
    return lines, (steps if keep_steps else None)


def compute_layout(nodes, root):
    """返回 id -> {x(DFS 列), depth}。"""
    pos = {}

    def walk(node, depth, next_x):
        if not node.children:
            pos[node.id] = (next_x, depth)
            return next_x + 1
        for child in node.children:
            next_x = walk(child, depth + 1, next_x)
        min_x = min(pos[c.id][0] for c in node.children)
        max_x = max(pos[c.id][0] for c in node.children)
        pos[node.id] = ((min_x + max_x) // 2, depth)
        return next_x

    walk(root, 0, 0)
    return pos


HTML_CSS = """
body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
       margin: 20px; color: #1f2933; background: #fafafa; }
h1 { font-size: 20px; margin: 0 0 6px; }
.meta { color: #52606d; font-size: 13px; margin-bottom: 12px; }
#controls { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
#slider { width: 420px; }
button { padding: 4px 12px; border: 1px solid #9aa5b1; border-radius: 4px;
         background: #fff; cursor: pointer; }
button:hover { background: #e4e7eb; }
#steplabel { font-size: 14px; font-weight: 600; }
#events { min-height: 22px; margin: 6px 0; }
.event { display: inline-block; margin: 2px 8px 2px 0; padding: 3px 10px;
         border: 1px solid #c0392b; border-radius: 4px; color: #c0392b;
         background: #fdecea; font-size: 13px; }
#timeline { margin: 8px 0 4px; line-height: 0; }
#timeline span { display: inline-block; width: 10px; height: 14px; margin-right: 2px;
                 background: #cbd2d9; cursor: pointer; }
#timeline span.reclaim { background: #c0392b; }
#timeline span.current { outline: 2px solid #1f2933; }
svg { background: #fff; border: 1px solid #e4e7eb; border-radius: 6px;
      max-width: 100%; height: auto; }
.legend { font-size: 12px; color: #52606d; margin: 8px 0; }
.legend i { display: inline-block; width: 12px; height: 12px; margin: 0 4px 0 12px;
            vertical-align: -2px; border-radius: 2px; }
.node text { font-size: 11px; }
.node .nid { font-size: 13px; font-weight: 700; }
.debt-label { font-size: 10px; fill: #c0392b; paint-order: stroke;
              stroke: #fff; stroke-width: 3px; }
""".strip()


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>配额树模拟报告 - __TREE__</title>
<style>__CSS__</style>
</head>
<body>
<h1>配额树模拟报告</h1>
<div class="meta">树 <b>__TREE__</b> · 超时阈值 T = __TIMEOUT__ · 共 __NSTEPS__ 步 ·
节点数 __NNODES__ · 由 quota_sim.py 生成，所有数值均来自引擎输出</div>
<div id="controls">
  <button id="prev" type="button">上一步</button>
  <input id="slider" type="range" min="0" max="__SLIDERMAX__" value="0">
  <button id="next" type="button">下一步</button>
  <span id="steplabel"></span>
</div>
<div id="timeline"></div>
<div id="events"></div>
<div class="legend">
  <i style="background:#3e5c76"></i>自用 own
  <i style="background:#7fb069"></i>借出 lent
  <i style="background:#e8c547"></i>空闲 idle
  <i style="background:#e07a5f"></i>欠入 debt
  <span style="margin-left:12px">红色箭头 = 当前未还借用记录；时间轴红块 = 该步发生超时收回。</span>
</div>
<svg id="canvas" viewBox="0 0 __WIDTH__ __HEIGHT__"></svg>
<script>
"use strict";
const DATA = __DATA__;
const BOX_W = 158, BOX_H = 72, DX = 190, DY = 140;
const svg = document.getElementById("canvas");
const pos = {};
DATA.nodes.forEach(function(n) { pos[n.id] = n; });
function center(n) {
  return { x: 30 + n.x * DX + BOX_W / 2, y: 24 + n.depth * DY + BOX_H / 2 };
}
function esc(s) {
  return String(s).replace(/[&<>"]/g, function(c) {
    return {"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;"}[c];
  });
}
function bar(x, y, frac, fill) {
  const w = Math.max(0, Math.min(1, frac)) * 138;
  return '<rect x="' + x + '" y="' + y + '" width="' + w + '" height="6" '
         + 'fill="' + fill + '"></rect>';
}
function render(i) {
  const step = DATA.steps[i];
  document.getElementById("slider").value = i;
  const label = "t=" + step.t + " · " + step.id + " · " + step.action + " "
      + step.node + " " + step.amount + " · " + step.result + " (" + step.detail + ")";
  document.getElementById("steplabel").textContent = label;
  const ev = document.getElementById("events");
  ev.innerHTML = step.events.map(function(e) {
    return '<span class="event">EVENT t=' + step.t + " RECLAIM " + e.id + " "
        + esc(e.borrower) + " \u2192 " + esc(e.lender) + " 收回 " + e.amount
        + "（起始 t=" + e.start + "）</span>";
  }).join("");
  let s = '<defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" '
        + 'markerWidth="8" markerHeight="8" orient="auto-start-reverse">'
        + '<path d="M0,0 L10,5 L0,10 z" fill="#c0392b"></path></marker></defs>';
  DATA.nodes.forEach(function(n) {
    if (n.parent === null) return;
    const a = center(n), b = center(pos[n.parent]);
    s += '<line x1="' + a.x + '" y1="' + (a.y - BOX_H / 2) + '" x2="' + b.x
       + '" y2="' + (b.y + BOX_H / 2) + '" stroke="#9aa5b1" '
       + 'stroke-width="1.5"></line>';
  });
  step.debts.forEach(function(d) {
    const a = center(pos[d.borrower]), b = center(pos[d.lender]);
    const dx = b.x - a.x, dy = b.y - a.y;
    const len = Math.sqrt(dx * dx + dy * dy) || 1;
    const ux = dx / len, uy = dy / len;
    const sx = a.x + ux * (BOX_W / 2), sy = a.y + uy * (BOX_H / 2);
    const tx = b.x - ux * (BOX_W / 2 + 6), ty = b.y - uy * (BOX_H / 2 + 6);
    const mx = (sx + tx) / 2 - uy * 26, my = (sy + ty) / 2 + ux * 26;
    s += '<path d="M' + sx + ',' + sy + ' Q' + mx + ',' + my + ' ' + tx + ','
       + ty + '" fill="none" stroke="#c0392b" stroke-width="1.6" '
       + 'marker-end="url(#arr)"></path>';
    s += '<text class="debt-label" x="' + mx + '" y="' + my + '" '
       + 'text-anchor="middle">' + d.id + " " + d.remain + "/" + d.amount
       + '</text>';
  });
  DATA.nodes.forEach(function(n) {
    const c = center(n);
    const x = c.x - BOX_W / 2, y = c.y - BOX_H / 2;
    const st = step.state[n.id];
    const used = st[0], own = st[1], debt = st[2], lent = st[3];
    s += '<g class="node"><rect x="' + x + '" y="' + y + '" width="' + BOX_W
       + '" height="' + BOX_H + '" rx="6" fill="#ffffff" stroke="#52606d">';
    s += "</rect>";
    s += '<text class="nid" x="' + (x + 8) + '" y="' + (y + 16) + '">'
       + esc(n.id) + "</text>";
    s += '<text x="' + (x + BOX_W - 8) + '" y="' + (y + 16) + '" text-anchor="end">'
       + used + "/" + n.quota + "</text>";
    s += bar(x + 10, y + 22, own / n.quota, "#3e5c76");
    if (own < n.quota) s += bar(x + 10 + Math.min(1, own / n.quota) * 138, y + 22,
       Math.min(1, lent / n.quota), "#7fb069");
    s += '<rect x="' + (x + 10) + '" y="' + (y + 32) + '" width="'
       + (Math.min(1, debt / n.quota) * 138) + '" height="6" fill="#e07a5f"></rect>';
    s += '<text x="' + (x + 8) + '" y="' + (y + 52) + '">own=' + own
       + " debt=" + debt + "</text>";
    s += '<text x="' + (x + 8) + '" y="' + (y + 66) + '">lent=' + lent
       + " idle=" + (n.quota - own - lent) + "</text></g>";
  });
  svg.innerHTML = s;
  Array.prototype.forEach.call(document.querySelectorAll("#timeline span"),
      function(el, k) {
        el.classList.toggle("current", k === i);
      });
}
(function init() {
  const tl = document.getElementById("timeline");
  tl.innerHTML = DATA.steps.map(function(s) {
    return '<span title="t=' + s.t + " " + s.id + '"'
        + (s.events.length ? ' class="reclaim"' : "") + "></span>";
  }).join("");
  Array.prototype.forEach.call(tl.children, function(el, k) {
    el.addEventListener("click", function() { render(k); });
  });
  document.getElementById("prev").addEventListener("click", function() {
    const i = parseInt(document.getElementById("slider").value, 10);
    if (i > 0) render(i - 1);
  });
  document.getElementById("next").addEventListener("click", function() {
    const i = parseInt(document.getElementById("slider").value, 10);
    if (i < DATA.steps.length - 1) render(i + 1);
  });
  document.getElementById("slider").addEventListener("input", function(e) {
    render(parseInt(e.target.value, 10));
  });
  render(0);
})();
</script>
</body>
</html>
"""


def build_html(tree_name, timeout, nodes, steps):
    layout = compute_layout(nodes, _root_of(nodes))
    node_data = []
    max_x = 0
    max_depth = 0
    for n in nodes:
        x, depth = layout[n.id]
        max_x = max(max_x, x)
        max_depth = max(max_depth, depth)
        node_data.append({
            "id": n.id,
            "parent": n.parent.id if n.parent else None,
            "quota": n.quota,
            "x": x,
            "depth": depth,
        })
    data = {
        "tree": tree_name,
        "timeout": timeout,
        "nodes": node_data,
        "steps": steps,
    }
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("</", "<\\/")
    width = 30 + (max_x + 1) * 190 + 30
    height = 24 + (max_depth + 1) * 140 + 24
    html_text = HTML_TEMPLATE
    html_text = html_text.replace("__TREE__", tree_name)
    html_text = html_text.replace("__CSS__", HTML_CSS)
    html_text = html_text.replace("__TIMEOUT__", str(timeout))
    html_text = html_text.replace("__NSTEPS__", str(len(steps)))
    html_text = html_text.replace("__NNODES__", str(len(nodes)))
    html_text = html_text.replace("__SLIDERMAX__", str(len(steps) - 1))
    html_text = html_text.replace("__WIDTH__", str(width))
    html_text = html_text.replace("__HEIGHT__", str(height))
    html_text = html_text.replace("__DATA__", payload)
    return html_text


def _root_of(nodes):
    cur = nodes[0]
    while cur.parent is not None:
        cur = cur.parent
    return cur


def main(argv=None):
    parser = argparse.ArgumentParser(description="配额树继承与借用模拟器")
    parser.add_argument("--trees", required=True, help="树文件目录")
    parser.add_argument("--script", required=True, help="操作脚本路径")
    parser.add_argument("--html", help="HTML 报告输出路径（可选）")
    args = parser.parse_args(argv)

    try:
        tree_name, tree_lineno, timeout, ops = load_script(args.script)
        tree_path = os.path.join(args.trees, tree_name + ".tree")
        tree_dir = os.path.abspath(args.trees)
        if not os.path.abspath(tree_path).startswith(tree_dir + os.sep):
            fail(tree_lineno, "树名越出树目录: %s" % tree_name)
        nodes, by_id, _root = load_tree(tree_path)
        for op in ops:
            if op.node not in by_id:
                fail(op.lineno, "操作引用未知节点: %s" % op.node)
        lines, steps = simulate(nodes, by_id, timeout, ops,
                                keep_steps=bool(args.html))
    except InputError as exc:
        print("ERROR %d %s" % (exc.line, exc.message), file=sys.stderr)
        return 2

    sys.stdout.write("\n".join(lines))
    if lines:
        sys.stdout.write("\n")

    if args.html:
        report = build_html(tree_name, timeout, nodes, steps)
        parent = os.path.dirname(os.path.abspath(args.html))
        os.makedirs(parent, exist_ok=True)
        with open(args.html, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
