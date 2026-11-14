#!/usr/bin/env python3
"""台账核心逻辑测试：分数精度、原子性、追溯、召回、历史回看、CSV 导出。

运行：python3 -I -m unittest discover -s tests -v
"""

import csv
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ledger import (  # noqa: E402
    ConflictError,
    Ledger,
    LedgerError,
    NotFoundError,
    parse_qty,
    qty_display,
    qty_str,
)


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "ledger.sqlite3"
        self.L = Ledger(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def tubes(self):
        return {t["code"]: t for t in self.L.inventory()["tubes"]}


class TestQuantityParsing(unittest.TestCase):
    def test_decimal_is_exact(self):
        self.assertEqual(parse_qty("0.1") + parse_qty("0.2"), parse_qty("0.3"))
        self.assertEqual(parse_qty(0.1) + parse_qty(0.2), parse_qty(0.3))

    def test_fraction_literal(self):
        self.assertEqual(parse_qty("1/3"), parse_qty(1) / 3)
        self.assertEqual(qty_str(parse_qty("2/6")), "1/3")
        self.assertEqual(qty_display(parse_qty("2/6")), "1/3")

    def test_rejects_bad_values(self):
        for bad in ["0", "-1", "abc", "", None, "1/0", "nan", "inf", True]:
            with self.assertRaises(LedgerError):
                parse_qty(bad)

    def test_display_terminating(self):
        self.assertEqual(qty_display(parse_qty("12.5")), "12.5")
        self.assertEqual(qty_display(parse_qty("1.25")), "1.25")


class TestIntakeAndSplit(LedgerCase):
    def test_intake_and_composition(self):
        op = self.L.register_intake("A-1", "LOT-A", "1000")
        self.assertEqual(op["kind"], "intake")
        self.assertEqual(op["outputs"][0]["code"], "A-1")
        tube = self.tubes()["A-1"]
        self.assertEqual(tube["remaining_display"], "1000")
        self.assertEqual(tube["composition"][0]["percent"], "100")

    def test_duplicate_code_rejected_case_insensitive(self):
        self.L.register_intake("A-1", "LOT-A", "10")
        with self.assertRaises(ConflictError):
            self.L.register_intake("a-1", "LOT-B", "10")
        self.assertEqual(len(self.tubes()), 1)

    def test_mixing_proportions(self):
        self.L.register_intake("A", "LOT-A", "1000")
        self.L.register_intake("C", "LOT-C", "500")
        op = self.L.split(
            [{"tube": "A", "amount": "200"}, {"tube": "C", "amount": "100"}],
            [{"code": "M1", "amount": "300"}],
        )
        self.assertEqual(op["total_in"], "300")
        self.assertEqual(op["total_out"], "300")
        sources = op["outputs"][0]["sources"]
        self.assertEqual(sources[0]["amount"], "200")
        self.assertEqual(sources[1]["amount"], "100")
        m1 = self.tubes()["M1"]
        self.assertEqual(m1["initial_volume"], "300")
        shares = {c["intake_code"]: c["share"] for c in m1["composition"]}
        self.assertEqual(shares["A"], "2/3")
        self.assertEqual(shares["C"], "1/3")

    def test_each_output_gets_own_breakdown(self):
        self.L.register_intake("A", "LOT-A", "1000")
        op = self.L.split(
            [{"tube": "A", "amount": "1/3"}],
            [{"code": "S1", "amount": "1/3"}],
        )
        self.assertEqual(op["outputs"][0]["amount"], "1/3")
        self.assertEqual(op["outputs"][0]["sources"][0]["amount"], "1/3")
        tube = self.tubes()["S1"]
        self.assertEqual(tube["initial_volume"], "1/3")

    def test_fractional_volumes_stay_exact_through_chain(self):
        self.L.register_intake("A", "LOT-A", "1000")
        self.L.split(
            [{"tube": "A", "amount": "100"}],
            [
                {"code": "S1", "amount": "1/3"},
                {"code": "S2", "amount": "299/3"},
            ],
        )
        # 从 S2 再分一份 1/7，来源占比应精确为分数
        op2 = self.L.split(
            [{"tube": "S2", "amount": "1/7"}],
            [{"code": "S3", "amount": "1/7"}],
        )
        self.assertEqual(op2["total_in"], "1/7")
        s3 = self.tubes()["S3"]
        self.assertEqual(s3["initial_volume"], "1/7")
        self.assertEqual(s3["composition"][0]["share"], "1")

    def test_total_mismatch_rolls_back(self):
        self.L.register_intake("A", "LOT-A", "1000")
        before = self.L.inventory()
        with self.assertRaises(LedgerError) as ctx:
            self.L.split(
                [{"tube": "A", "amount": "10"}],
                [{"code": "S1", "amount": "11"}],
            )
        self.assertIn("不一致", ctx.exception.message)
        self.assertEqual(self.L.inventory()["latest_seq"], before["latest_seq"])
        self.assertNotIn("S1", self.tubes())
        self.assertEqual(self.tubes()["A"]["remaining"], "1000")

    def test_insufficient_volume_rolls_back_all_lines(self):
        self.L.register_intake("A", "LOT-A", "100")
        self.L.register_intake("B", "LOT-B", "100")
        before = self.L.inventory()
        with self.assertRaises(LedgerError) as ctx:
            self.L.split(
                [
                    {"tube": "A", "amount": "50"},
                    {"tube": "B", "amount": "500"},
                ],
                [{"code": "S1", "amount": "550"}],
            )
        details = " ".join(ctx.exception.details)
        self.assertIn("用量不足", details)
        after = self.L.inventory()
        self.assertEqual(after["latest_seq"], before["latest_seq"])
        self.assertEqual(self.tubes()["A"]["remaining"], "100")
        self.assertEqual(self.tubes()["B"]["remaining"], "100")
        self.assertNotIn("S1", self.tubes())

    def test_duplicate_output_code_rolls_back_inputs(self):
        self.L.register_intake("A", "LOT-A", "1000")
        self.L.register_intake("X", "LOT-X", "10")
        with self.assertRaises(LedgerError) as ctx:
            self.L.split(
                [{"tube": "A", "amount": "5"}],
                [{"code": "X", "amount": "5"}],
            )
        self.assertIn("已存在", " ".join(ctx.exception.details))
        self.assertEqual(self.tubes()["A"]["remaining"], "1000")
        self.assertEqual(self.tubes()["X"]["remaining"], "10")

    def test_bad_input_field_rolls_back(self):
        self.L.register_intake("A", "LOT-A", "1000")
        with self.assertRaises(LedgerError):
            self.L.split(
                [{"tube": "A", "amount": "5"}, {"tube": "nope", "amount": "5"}],
                [{"code": "S1", "amount": "10"}],
            )
        self.assertEqual(self.tubes()["A"]["remaining"], "1000")
        self.assertNotIn("S1", self.tubes())

    def test_mid_write_failure_rolls_back(self):
        """模拟写库过程中崩溃：不能留下扣了量却没有成品的半截状态。"""
        self.L.register_intake("A", "LOT-A", "1000")

        def boom(stage, index=0):
            if stage == "split-output" and index == 1:
                raise RuntimeError("模拟磁盘故障")

        self.L._fault_injector = boom
        with self.assertRaises(RuntimeError):
            self.L.split(
                [{"tube": "A", "amount": "100"}],
                [{"code": "S1", "amount": "50"}, {"code": "S2", "amount": "50"}],
            )
        self.L._fault_injector = None
        inv = self.L.inventory()
        self.assertEqual(inv["latest_seq"], 1)
        self.assertEqual(self.tubes()["A"]["remaining"], "1000")
        self.assertNotIn("S1", self.tubes())
        self.assertNotIn("S2", self.tubes())

    def test_exhaustion_then_blocked(self):
        self.L.register_intake("A", "LOT-A", "100")
        self.L.split([{"tube": "A", "amount": "100"}], [{"code": "S1", "amount": "100"}])
        self.assertEqual(self.tubes()["A"]["status"], "exhausted")
        with self.assertRaises(LedgerError):
            self.L.split([{"tube": "A", "amount": "1"}], [{"code": "S2", "amount": "1"}])
        self.assertEqual(self.tubes()["S1"]["status"], "available")

    def test_full_amount_ok_and_exact_remainder(self):
        self.L.register_intake("A", "LOT-A", "1/3")
        self.L.split([{"tube": "A", "amount": "1/3"}], [{"code": "S1", "amount": "1/3"}])
        self.assertEqual(self.tubes()["A"]["remaining"], "0")
        self.assertEqual(self.tubes()["A"]["status"], "exhausted")

    def test_sample_same_tube_twice_rejected(self):
        self.L.register_intake("A", "LOT-A", "100")
        with self.assertRaises(LedgerError) as ctx:
            self.L.split(
                [{"tube": "A", "amount": "10"}, {"tube": "a", "amount": "10"}],
                [{"code": "S1", "amount": "20"}],
            )
        self.assertIn("重复", " ".join(ctx.exception.details))

    def test_output_code_equal_input_code_rejected(self):
        self.L.register_intake("A", "LOT-A", "100")
        with self.assertRaises(LedgerError):
            self.L.split([{"tube": "A", "amount": "10"}], [{"code": "A", "amount": "10"}])
        self.assertEqual(self.tubes()["A"]["remaining"], "100")

    def test_nested_pool_composition(self):
        self.L.register_intake("A", "LOT-A", "1000")
        self.L.register_intake("B", "LOT-B", "1000")
        self.L.split(
            [{"tube": "A", "amount": "100"}, {"tube": "B", "amount": "100"}],
            [{"code": "M1", "amount": "200"}],
        )
        self.L.split([{"tube": "M1", "amount": "100"}], [{"code": "M2", "amount": "100"}])
        m2 = self.tubes()["M2"]
        shares = {c["intake_code"]: c["share"] for c in m2["composition"]}
        self.assertEqual(shares, {"A": "1/2", "B": "1/2"})


class TestTraceAndRecall(LedgerCase):
    def build(self):
        self.L.register_intake("A", "LOT-A", "1000")
        self.L.register_intake("B", "LOT-B", "800")
        self.L.split(
            [{"tube": "A", "amount": "400"}, {"tube": "B", "amount": "200"}],
            [
                {"code": "M1", "amount": "300"},
                {"code": "M2", "amount": "300"},
            ],
        )
        self.L.split([{"tube": "M1", "amount": "100"}], [{"code": "M3", "amount": "100"}])

    def test_tube_trace_composition_and_lineage(self):
        self.build()
        data = self.L.tube_trace("M3")
        self.assertEqual(data["tube"]["composition"][0]["intake_code"], "A")
        line = data["lineage"]
        self.assertEqual(line["code"], "M3")
        self.assertEqual([c["code"] for c in line["children"]], ["M1"])
        self.assertEqual(
            [c["code"] for c in line["children"][0]["children"]], ["A", "B"]
        )
        self.assertEqual(data["used_in"], [])

    def test_tube_trace_shows_uses(self):
        self.build()
        data = self.L.tube_trace("A")
        self.assertEqual(len(data["used_in"]), 1)
        self.assertEqual(
            sorted(o["code"] for o in data["used_in"][0]["outputs"]), ["M1", "M2"]
        )

    def test_batch_trace_totals(self):
        self.build()
        data = self.L.batch_trace("LOT-A")
        by_code = {t["code"]: t for t in data["tubes"]}
        self.assertEqual(by_code["A"]["batch_amount_initial"], "1000")
        self.assertEqual(by_code["A"]["batch_amount_remaining"], "600")
        self.assertEqual(by_code["M1"]["batch_share_percent"], "66.6667")
        self.assertEqual(by_code["M1"]["batch_amount_initial"], "200")
        self.assertIn("M3", by_code)
        self.assertEqual(by_code["M3"]["batch_share"], "2/3")

    def test_batch_trace_unknown(self):
        with self.assertRaises(NotFoundError):
            self.L.batch_trace("NOPE")

    def test_recall_marks_only_in_stock(self):
        self.build()
        # 把 M2 用尽
        self.L.split([{"tube": "M2", "amount": "300"}], [{"code": "M4", "amount": "300"}])
        result = self.L.recall("LOT-A", "来样异常")
        codes = [a["code"] for a in result["recall"]["affected"]]
        # M4 由 M2 分出，仍含 LOT-A 且在库，也应被标记
        self.assertEqual(set(codes), {"A", "M1", "M3", "M4"})
        self.assertNotIn("M2", codes)
        inv = self.tubes()
        self.assertTrue(inv["A"]["recalled"])
        self.assertFalse(inv["B"]["recalled"])
        self.assertTrue(inv["M2"]["recalled"] is False)
        with self.assertRaises(LedgerError):
            self.L.split([{"tube": "A", "amount": "1"}], [{"code": "Z", "amount": "1"}])
        # 其他批次照常使用
        self.L.split([{"tube": "B", "amount": "10"}], [{"code": "Z", "amount": "10"}])

    def test_recall_history_snapshot(self):
        self.build()
        res = self.L.recall("LOT-A", "发现问题")
        rid = res["recall"]["id"]
        self.assertEqual(res["recall"]["affected"][0]["remaining"], "600")
        # 后续在未召回的其他批次上操作，召回记录仍显示 M1 当时用量
        self.L.split([{"tube": "B", "amount": "50"}], [{"code": "M5", "amount": "50"}])
        detail = self.L.recall_detail(rid)
        m1 = next(a for a in detail["affected"] if a["code"] == "M1")
        self.assertEqual(m1["remaining"], "200")
        self.assertEqual(m1["current_remaining"], "200")
        self.assertEqual(detail["affected_count"], 4)

    def test_recall_bad_batch(self):
        with self.assertRaises(NotFoundError):
            self.L.recall("NOPE")

    def test_retire_and_discard(self):
        self.build()
        self.L.retire(["M1"], "污染")
        m1 = self.tubes()["M1"]
        self.assertEqual(m1["status"], "retired")
        self.assertEqual(m1["discarded"], "200")
        self.assertEqual(m1["remaining"], "200")
        with self.assertRaises(LedgerError):
            self.L.split([{"tube": "M1", "amount": "1"}], [{"code": "Z", "amount": "1"}])
        with self.assertRaises(LedgerError):
            self.L.retire(["M1"], "再来一次")

    def test_retire_rolls_back_when_one_missing(self):
        self.build()
        with self.assertRaises(LedgerError):
            self.L.retire(["M1", "NOPE"])
        self.assertEqual(self.tubes()["M1"]["status"], "available")
        self.assertEqual(self.L.inventory()["counts"]["retired"], 0)


class TestReversal(LedgerCase):
    """冲销：整批作废误录的分装及依赖它的后续操作，误扣用量退回原管。"""

    def build_chain(self):
        """A --100--> S1 --60--> S2；另有一支无关的 B。"""
        self.L.register_intake("A", "LOT-A", "1000")
        self.L.register_intake("B", "LOT-B", "500")
        self.L.split([{"tube": "A", "amount": "100"}], [{"code": "S1", "amount": "100"}])
        self.L.split([{"tube": "S1", "amount": "60"}], [{"code": "S2", "amount": "60"}])

    def reverse(self, op, note=""):
        token = self.L.reverse_preview(op)["token"]
        return self.L.reverse(op, token, note)

    def test_preview_is_read_only(self):
        self.build_chain()
        before = self.L.inventory()
        preview = self.L.reverse_preview(3)
        after = self.L.inventory()
        self.assertEqual(before["latest_seq"], after["latest_seq"])
        self.assertEqual(preview["root_op"], 3)
        self.assertTrue(preview["ok"])
        self.assertEqual([op["id"] for op in preview["void_ops"]], [3, 4])
        self.assertEqual([t["code"] for t in preview["doomed_tubes"]], ["S1", "S2"])
        self.assertEqual(len(preview["refunds"]), 1)
        self.assertEqual(preview["refunds"][0]["code"], "A")
        self.assertEqual(preview["refunds"][0]["amount"], "100")
        self.assertEqual(preview["refunds"][0]["remaining_after"], "1000")

    def test_reverse_cascades_and_refunds_source_once(self):
        self.build_chain()
        result = self.reverse(3, "分装录错")
        rev = result["reversal"]
        self.assertEqual(rev["root_op"], 3)
        self.assertEqual([v["op_id"] for v in rev["void_ops"]], [3, 4])
        # S1→S2 的 60 µL 属于被作废链条内部倒手，不退；只退 A 被误扣的 100
        self.assertEqual([(r["code"], r["amount"]) for r in rev["refunds"]], [("A", "100")])
        tubes = self.tubes()
        self.assertNotIn("S1", tubes)
        self.assertNotIn("S2", tubes)
        self.assertEqual(tubes["A"]["remaining"], "1000")
        self.assertEqual(tubes["A"]["status"], "available")
        # 无关的 B 照旧
        self.assertEqual(tubes["B"]["remaining"], "500")

    def test_history_keeps_pre_reversal_accounts(self):
        self.build_chain()
        self.reverse(3)
        # 当前账：A 已退回，S1/S2 不存在
        self.assertEqual(self.tubes()["A"]["remaining"], "1000")
        # 回看冲销前的账：操作 3 完成时 A=900、S1 在库 100；操作 4 完成时 S1=40、S2 在库
        snap3 = self.L.state_at(3)
        by_code = {t["code"]: t for t in snap3["tubes"]}
        self.assertEqual(by_code["A"]["remaining"], "900")
        self.assertEqual(by_code["S1"]["remaining"], "100")
        snap4 = self.L.state_at(4)
        by_code4 = {t["code"]: t for t in snap4["tubes"]}
        self.assertEqual(by_code4["S1"]["remaining"], "40")
        self.assertIn("S2", by_code4)
        # 历史里三笔操作都在，被冲销的标注 voided_by，冲销操作自带明细
        hist = self.L.history()["operations"]
        self.assertEqual([op["kind"] for op in hist], ["intake", "intake", "split", "split", "reverse"])
        self.assertEqual(hist[2]["voided_by"], 5)
        self.assertEqual(hist[3]["voided_by"], 5)
        self.assertIsNone(hist[4]["voided_by"])
        self.assertEqual(hist[4]["reversal"]["root_op"], 3)
        # 冲销前的单管追溯也能回看
        past = self.L.tube_trace("S1", seq=3)
        self.assertEqual(past["tube"]["remaining"], "100")

    def test_voided_tube_cannot_be_used(self):
        self.build_chain()
        self.reverse(3)
        with self.assertRaises(NotFoundError) as ctx:
            self.L.tube_trace("S1")
        self.assertIn("冲销", str(ctx.exception.message))
        with self.assertRaises(LedgerError) as ctx:
            self.L.split([{"tube": "S1", "amount": "1"}], [{"code": "Z", "amount": "1"}])
        self.assertIn("已被冲销", " ".join(ctx.exception.details))
        with self.assertRaises(LedgerError) as ctx:
            self.L.retire(["S1"])
        self.assertIn("已被冲销", " ".join(ctx.exception.details))

    def test_retire_item_on_doomed_tube_voided_but_others_stay(self):
        self.build_chain()
        self.L.retire(["S2", "B"], "一起报废")  # 操作 5：同一单报废两管
        result = self.reverse(3)
        rev = result["reversal"]
        self.assertEqual([(i["op_id"], i["code"]) for i in rev["void_retire_items"]], [(5, "S2")])
        tubes = self.tubes()
        self.assertNotIn("S2", tubes)
        # 同一报废单里的 B 不受影响，仍是已报废
        self.assertEqual(tubes["B"]["status"], "retired")
        self.assertEqual(tubes["B"]["retired_op"], 5)
        # 报废操作本身仍在，只有 S2 那条明细被作废
        op5 = self.L.operation_detail(5)
        by_code = {r["code"]: r for r in op5["retired"]}
        self.assertEqual(by_code["S2"]["voided_by"], 6)
        self.assertIsNone(by_code["B"]["voided_by"])

    def test_independent_splits_untouched(self):
        self.build_chain()
        self.L.split([{"tube": "B", "amount": "50"}], [{"code": "T1", "amount": "50"}])
        self.reverse(3)
        tubes = self.tubes()
        self.assertEqual(tubes["B"]["remaining"], "450")
        self.assertEqual(tubes["T1"]["remaining"], "50")

    def test_exhausted_source_becomes_available_again(self):
        self.L.register_intake("A", "LOT-A", "100")
        self.L.split([{"tube": "A", "amount": "60"}], [{"code": "S1", "amount": "60"}])
        self.L.split([{"tube": "A", "amount": "40"}], [{"code": "S2", "amount": "40"}])
        self.assertEqual(self.tubes()["A"]["status"], "exhausted")
        self.reverse(2)
        a = self.tubes()["A"]
        self.assertEqual(a["remaining"], "60")
        self.assertEqual(a["status"], "available")
        self.assertEqual(self.tubes()["S2"]["remaining"], "40")

    def test_blocked_when_refund_source_retired(self):
        self.build_chain()
        self.L.retire(["A"], "原管废弃")
        preview = self.L.reverse_preview(3)
        self.assertFalse(preview["ok"])
        self.assertIn("已报废", " ".join(preview["problems"]))
        with self.assertRaises(LedgerError) as ctx:
            self.reverse(3)
        self.assertIn("已报废", " ".join(ctx.exception.details))
        # 整笔拒绝：账目不变
        tubes = self.tubes()
        self.assertEqual(tubes["A"]["remaining"], "900")
        self.assertEqual(tubes["A"]["status"], "retired")
        self.assertIn("S1", tubes)
        self.assertEqual(self.L.inventory()["latest_seq"], 5)

    def test_refund_to_recalled_source_stays_blocked(self):
        self.build_chain()
        self.L.recall("LOT-A", "来样异常")  # 操作 5
        preview = self.L.reverse_preview(3)
        self.assertTrue(preview["ok"])  # 召回不阻止冲销
        refund = preview["refunds"][0]
        self.assertTrue(refund["recalled"])
        self.reverse(3)
        a = self.tubes()["A"]
        self.assertEqual(a["remaining"], "1000")  # 误扣用量已退回
        self.assertTrue(a["recalled"])  # 已发召回继续有效
        # 被召回的样品退回后也不能取用
        with self.assertRaises(LedgerError) as ctx:
            self.L.split([{"tube": "A", "amount": "1"}], [{"code": "Z", "amount": "1"}])
        self.assertIn("召回", " ".join(ctx.exception.details))
        # 召回记录仍在，被冲销的管标注为已失效
        recall = self.L.recall_detail(1)
        by_code = {x["code"]: x for x in recall["affected"]}
        self.assertEqual(by_code["S1"]["current_status"], "voided")
        self.assertEqual(by_code["S1"]["remaining"], "40")  # 当时快照不变
        self.assertEqual(by_code["A"]["current_status"], "available")

    def test_duplicate_submission_no_double_refund(self):
        self.build_chain()
        token = self.L.reverse_preview(3)["token"]
        self.L.reverse(3, token)
        with self.assertRaises(ConflictError) as ctx:
            self.L.reverse(3, token)
        self.assertIn("已被冲销", ctx.exception.message)
        self.assertEqual(self.tubes()["A"]["remaining"], "1000")
        # 换一个令牌再提交也一样
        with self.assertRaises(ConflictError):
            self.reverse(3)
        self.assertEqual(self.tubes()["A"]["remaining"], "1000")

    def test_reversing_voided_downstream_op_conflicts(self):
        self.build_chain()
        self.reverse(3)  # 级联把操作 4 也冲销了
        with self.assertRaises(ConflictError):
            self.reverse(4)

    def test_token_invalidated_by_relevant_change(self):
        self.build_chain()
        token = self.L.reverse_preview(3)["token"]
        # 预览后又从原管 A 取了一枪：相关库存变动，令牌失效
        self.L.split([{"tube": "A", "amount": "10"}], [{"code": "X", "amount": "10"}])
        with self.assertRaises(ConflictError) as ctx:
            self.L.reverse(3, token)
        self.assertIn("重新预览", ctx.exception.message)
        # 重新预览后令牌更新，可以提交
        self.reverse(3)
        self.assertEqual(self.tubes()["A"]["remaining"], "990")
        self.assertEqual(self.tubes()["X"]["remaining"], "10")

    def test_token_invalidated_by_recall_on_source(self):
        self.build_chain()
        token = self.L.reverse_preview(3)["token"]
        self.L.recall("LOT-A")
        with self.assertRaises(ConflictError):
            self.L.reverse(3, token)

    def test_unrelated_change_does_not_invalidate_token(self):
        self.build_chain()
        token = self.L.reverse_preview(3)["token"]
        self.L.register_intake("C", "LOT-C", "10")  # 无关来样
        self.L.split([{"tube": "B", "amount": "5"}], [{"code": "T", "amount": "5"}])  # 无关分装
        result = self.L.reverse(3, token)
        self.assertEqual(result["reversal"]["root_op"], 3)
        self.assertEqual(self.tubes()["A"]["remaining"], "1000")

    def test_rejects_non_split_operations(self):
        self.build_chain()
        self.L.retire(["B"])
        self.L.recall("LOT-A")
        for bad in (1, 5, 6):  # intake / retire / recall
            with self.assertRaises(LedgerError) as ctx:
                self.L.reverse_preview(bad)
            self.assertIn("只能冲销", ctx.exception.message)
        with self.assertRaises(NotFoundError):
            self.L.reverse_preview(99)
        # 冲销操作本身也不能再被冲销
        self.reverse(3)
        with self.assertRaises(LedgerError):
            self.L.reverse_preview(7)

    def test_missing_token_rejected(self):
        self.build_chain()
        with self.assertRaises(LedgerError):
            self.L.reverse(3, "")
        with self.assertRaises(LedgerError):
            self.L.reverse(3, None)

    def test_fractional_amounts_refunded_exactly(self):
        self.L.register_intake("A", "LOT-A", "1/3")
        self.L.split([{"tube": "A", "amount": "1/7"}], [{"code": "S1", "amount": "1/7"}])
        self.reverse(2)
        a = self.tubes()["A"]
        self.assertEqual(a["remaining"], "1/3")
        self.assertEqual(Fraction(a["remaining"]), Fraction(1, 3))

    def test_mid_write_failure_rolls_back(self):
        self.build_chain()

        def boom(stage, index=0):
            if stage == "reverse-refund":
                raise RuntimeError("模拟磁盘故障")

        self.L._fault_injector = boom
        token = self.L.reverse_preview(3)["token"]
        with self.assertRaises(RuntimeError):
            self.L.reverse(3, token)
        self.L._fault_injector = None
        # 半截状态不存在：账还是冲销前的账，可以重新提交
        tubes = self.tubes()
        self.assertEqual(tubes["A"]["remaining"], "900")
        self.assertIn("S1", tubes)
        self.assertEqual(self.L.inventory()["latest_seq"], 4)
        self.reverse(3)
        self.assertEqual(self.tubes()["A"]["remaining"], "1000")

    def test_reverse_downstream_first_then_upstream(self):
        self.build_chain()
        # 先冲销下游 op4：S1 退回 60，S2 消失
        self.reverse(4)
        tubes = self.tubes()
        self.assertEqual(tubes["S1"]["remaining"], "100")
        self.assertNotIn("S2", tubes)
        # 再冲销上游 op3：op4 已冲销不再重复退，只退 A 被误扣的 100
        preview = self.L.reverse_preview(3)
        self.assertEqual([op["id"] for op in preview["void_ops"]], [3])
        self.assertEqual(
            [(r["code"], r["amount"]) for r in preview["refunds"]], [("A", "100")]
        )
        self.reverse(3)
        tubes = self.tubes()
        self.assertEqual(tubes["A"]["remaining"], "1000")
        self.assertNotIn("S1", tubes)

    def test_reversal_persists_after_reopen(self):
        self.build_chain()
        self.reverse(3)
        reopened = Ledger(self.db)
        tubes = {t["code"]: t for t in reopened.inventory()["tubes"]}
        self.assertEqual(tubes["A"]["remaining"], "1000")
        self.assertNotIn("S1", tubes)
        hist = reopened.history()["operations"]
        self.assertEqual(hist[-1]["kind"], "reverse")
        self.assertEqual(hist[2]["voided_by"], 5)
        with self.assertRaises(ConflictError):
            reopened.reverse(3, reopened.reverse_preview(3)["token"])


class TestMigrationFromV1(unittest.TestCase):
    """旧版（v1）数据库打开时自动迁移，数据与自增序号都保留。"""

    V1_DDL = """
    CREATE TABLE operations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL CHECK (kind IN ('intake', 'split', 'retire', 'recall')),
        note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    );
    CREATE TABLE tubes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL COLLATE NOCASE UNIQUE,
        kind TEXT NOT NULL CHECK (kind IN ('intake', 'pooled')),
        batch TEXT,
        initial_volume TEXT NOT NULL,
        composition TEXT NOT NULL,
        created_op INTEGER NOT NULL REFERENCES operations(id),
        created_at TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE op_inputs (
        op_id INTEGER NOT NULL REFERENCES operations(id),
        tube_id INTEGER NOT NULL REFERENCES tubes(id),
        amount TEXT NOT NULL,
        position INTEGER NOT NULL,
        PRIMARY KEY (op_id, position)
    );
    CREATE TABLE op_outputs (
        op_id INTEGER NOT NULL REFERENCES operations(id),
        tube_id INTEGER NOT NULL REFERENCES tubes(id),
        amount TEXT NOT NULL,
        position INTEGER NOT NULL,
        PRIMARY KEY (op_id, position)
    );
    CREATE TABLE retire_items (
        op_id INTEGER NOT NULL REFERENCES operations(id),
        tube_id INTEGER NOT NULL REFERENCES tubes(id),
        discarded TEXT NOT NULL,
        position INTEGER NOT NULL,
        PRIMARY KEY (op_id, position)
    );
    CREATE TABLE recalls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        op_id INTEGER NOT NULL UNIQUE REFERENCES operations(id),
        batch TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    );
    CREATE TABLE recall_items (
        recall_id INTEGER NOT NULL REFERENCES recalls(id),
        tube_id INTEGER NOT NULL REFERENCES tubes(id),
        code TEXT NOT NULL,
        remaining TEXT NOT NULL,
        composition TEXT NOT NULL,
        position INTEGER NOT NULL,
        PRIMARY KEY (recall_id, position)
    );
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "ledger.sqlite3"
        conn = sqlite3.connect(self.db)
        conn.executescript(self.V1_DDL)
        conn.execute("PRAGMA user_version = 1")
        conn.execute(
            "INSERT INTO operations(kind, note, created_at) "
            "VALUES ('intake', '旧数据', '2026-11-01T09:00:00+08:00')"
        )
        conn.execute(
            "INSERT INTO tubes(code, kind, batch, initial_volume, composition, "
            "created_op, created_at, note) "
            "VALUES ('A', 'intake', 'LOT-A', '1000', '{\"1\": \"1\"}', 1, "
            "'2026-11-01T09:00:00+08:00', '')"
        )
        conn.execute(
            "INSERT INTO op_outputs(op_id, tube_id, amount, position) "
            "VALUES (1, 1, '1000', 0)"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_migrate_and_keep_working(self):
        L = Ledger(self.db)
        version = sqlite3.connect(self.db).execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 2)
        # 旧数据还在
        tubes = {t["code"]: t for t in L.inventory()["tubes"]}
        self.assertEqual(tubes["A"]["remaining"], "1000")
        # 自增序号延续（新操作应是 #2，而不是从 1 重来）
        op = L.split([{"tube": "A", "amount": "100"}], [{"code": "S1", "amount": "100"}])
        self.assertEqual(op["id"], 2)
        # 迁移后可以正常冲销
        token = L.reverse_preview(2)["token"]
        L.reverse(2, token)
        tubes = {t["code"]: t for t in L.inventory()["tubes"]}
        self.assertEqual(tubes["A"]["remaining"], "1000")
        self.assertNotIn("S1", tubes)
        # 再开一次仍然有效
        reopened = Ledger(self.db)
        self.assertEqual(reopened.inventory()["latest_seq"], 3)


class TestHistory(LedgerCase):
    def test_state_at_each_operation(self):
        self.L.register_intake("A", "LOT-A", "1000")
        self.L.split([{"tube": "A", "amount": "100"}], [{"code": "S1", "amount": "100"}])
        self.L.split([{"tube": "S1", "amount": "40"}], [{"code": "S2", "amount": "40"}])
        snap1 = self.L.state_at(1)
        self.assertEqual(len(snap1["tubes"]), 1)
        snap2 = self.L.state_at(2)
        self.assertEqual(snap2["tubes"][1]["remaining"], "100")
        self.assertNotIn("S2", {t["code"] for t in snap2["tubes"]})
        snap3 = self.L.state_at(3)
        self.assertEqual(
            next(t for t in snap3["tubes"] if t["code"] == "S1")["remaining"], "60"
        )

    def test_tube_trace_in_past(self):
        self.L.register_intake("A", "LOT-A", "1000")
        self.L.split([{"tube": "A", "amount": "100"}], [{"code": "S1", "amount": "100"}])
        past = self.L.tube_trace("A", seq=1)
        self.assertEqual(past["tube"]["remaining"], "1000")
        with self.assertRaises(NotFoundError):
            self.L.tube_trace("S1", seq=1)

    def test_history_order(self):
        self.L.register_intake("B", "LOT-B", "100")
        self.L.register_intake("A", "LOT-A", "100")
        hist = self.L.history()
        self.assertEqual([op["id"] for op in hist["operations"]], [1, 2])
        self.assertEqual([op["kind"] for op in hist["operations"]], ["intake", "intake"])

    def test_state_beyond_latest(self):
        self.L.register_intake("A", "LOT-A", "100")
        with self.assertRaises(NotFoundError):
            self.L.state_at(5)


class TestPersistenceAndExport(LedgerCase):
    def test_reopen_keeps_data(self):
        self.L.register_intake("A", "LOT-A", "1000")
        self.L.split([{"tube": "A", "amount": "100"}], [{"code": "S1", "amount": "100"}])
        self.L.recall("LOT-A", "问题")
        self.L = None
        reopened = Ledger(self.db)
        self.assertEqual(reopened.inventory()["latest_seq"], 3)
        self.assertEqual(len(reopened.recalls()["recalls"]), 1)
        self.assertTrue(reopened.tube_trace("S1")["tube"]["recalled"])

    def test_inventory_csv(self):
        self.L.register_intake("A", "LOT-A", "1000")
        self.L.split([{"tube": "A", "amount": "100"}], [{"code": "S1", "amount": "100"}])
        text = self.L.inventory_csv()
        rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
        self.assertEqual(rows[0][0], "编号")
        self.assertIn("A", [r[0] for r in rows])
        self.assertIn("S1", [r[0] for r in rows])

    def test_recalls_csv(self):
        self.L.register_intake("A", "LOT-A", "1000")
        self.L.split([{"tube": "A", "amount": "100"}], [{"code": "S1", "amount": "100"}])
        self.L.recall("LOT-A", "问题")
        text = self.L.recalls_csv()
        rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
        self.assertEqual(len(rows), 3)
        self.assertIn("S1", rows[2][5])

    def test_csv_escapes_commas(self):
        self.L.register_intake("A,1", "LOT-A", "10", note='含,逗号"和换行\n')
        text = self.L.inventory_csv()
        rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
        self.assertEqual(rows[1][0], "A,1")
        self.assertEqual(rows[1][-1], '含,逗号"和换行')


if __name__ == "__main__":
    unittest.main()
