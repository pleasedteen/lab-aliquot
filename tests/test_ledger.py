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
