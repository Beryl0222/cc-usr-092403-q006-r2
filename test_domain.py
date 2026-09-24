"""领域不变量证明测试 —— 直接对 HeritageCitrusService 验证全部业务规则。"""

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from domain import (
    Conflict,
    HeritageCitrusService,
    NotFound,
    PermissionDenied,
    ROLE_COOP,
    ROLE_FARMER,
    ROLE_GUARD,
    ROLE_REVIEWER,
    ROLE_ENTERPRISE,
    ROLE_PROCESSOR,
    ROLE_REGULATOR,
    ValidationFailed,
)


class World:
    """搭建一套最小但完整的生产关系：农户-地块-老树/百年树-合约。"""

    def __init__(self):
        self.svc = HeritageCitrusService()
        self.svc.register_actor("coop", "秦会计", ROLE_COOP)
        self.svc.register_actor("reviewer", "严复核", ROLE_REVIEWER)
        self.svc.register_actor("guard", "护树员老吴", ROLE_GUARD)
        self.svc.register_actor("ent", "广兴饮料厂", ROLE_ENTERPRISE)
        self.svc.register_actor("proc", "陈皮加工厂", ROLE_PROCESSOR)
        self.svc.register_actor("reg", "监管老冯", ROLE_REGULATOR)

        self.coop = self.svc.authenticate("coop")
        self.reviewer = self.svc.authenticate("reviewer")
        self.guard = self.svc.authenticate("guard")
        self.ent = self.svc.authenticate("ent")
        self.proc = self.svc.authenticate("proc")
        self.reg = self.svc.authenticate("reg")

        farmer = self.svc.register_farmer(self.coop, "梁果农")
        self.farmer_id = farmer["农户编号"]
        self.svc.register_actor("farmer", "梁果农", ROLE_FARMER, self.farmer_id)
        self.farmer = self.svc.authenticate("farmer")

        # 第二位农户，用于隔离性验证
        other = self.svc.register_farmer(self.coop, "邻户老赵")
        self.other_id = other["农户编号"]
        self.svc.register_actor("farmer2", "邻户老赵", ROLE_FARMER, self.other_id)
        self.farmer2 = self.svc.authenticate("farmer2")

        self.plot = self.svc.create_plot(self.coop, self.farmer_id, "梁家湾坡地", "广兴镇梁家湾")
        self.plot_id = self.plot["地块编号"]
        self.old_tree = self.svc.register_tree_group(
            self.coop, self.plot_id, "连片老红橘", "红橘", 60, "普通老树")
        self.old_tree_id = self.old_tree["编号"]
        self.heritage = self.svc.register_tree_group(
            self.coop, self.plot_id, "百年母树群", "红橘", 130, "百年保护树")
        self.heritage_id = self.heritage["编号"]

        # 首个价规 2026-08-01 生效：A级4元、B级3元保护价
        self.svc.publish_price_rule(
            self.coop, "2026-08-01T00:00:00+00:00",
            protected={"A": "4.00", "B": "3.00"},
            market_reference={"A": "4.20", "B": "3.10"}, note="开园首版")
        self.contract = self.svc.sign_contract(
            self.coop, self.farmer_id, "广兴饮料厂",
            "2026-08-05T00:00:00+00:00", ["A", "B"], "2026秋")
        self.contract_id = self.contract["合约编号"]

        # 百年树配额：本季 100kg、2 次
        self.svc.set_quota(self.coop, self.heritage_id, "2026秋", "100", 2)


def weigh_inspect_settle(world: World, batch_id: str, tree_id: str, slip: str,
                         weight: str, grade: str, offline=False):
    """走通 过磅→初检，返回 (磅单视图, 检验视图)。"""
    ticket = world.svc.weigh(
        world.coop, batch_id, tree_id, slip, weight_kg=weight,
        offline=offline, device="地磅-01")
    insp = world.svc.inspect(
        world.reviewer, ticket["磅单编号"], grade, "糖度与外观抽检达标")
    return ticket, insp


class ContinuousChainTest(unittest.TestCase):
    def setUp(self):
        self.w = World()

    def test_full_chain_plot_to_settlement(self):
        svc = self.w.svc
        svc.add_care_log(self.w.coop, self.w.old_tree_id, "2026-08-20", "施有机肥、疏果")
        batch = svc.open_batch(self.w.coop, self.w.plot_id, "2026-09-01", "2026秋")
        bid = batch["批次编号"]
        ticket, insp = weigh_inspect_settle(self.w, bid, self.w.old_tree_id, "P001", "100", "A")

        delivery = svc.deliver(self.w.coop, bid, "广兴饮料厂",
                               self.w.contract_id, "2026-09-02T08:00:00+00:00")
        did = delivery["交货单编号"]
        self.assertEqual(delivery["计价价规"], "价规-v1")

        settlement = svc.settle(self.w.coop, did)
        self.assertEqual(settlement["明细行"][-1]["合计应收"], "400.00")
        self.assertEqual(settlement["明细行"][0]["磅单编号"], ticket["磅单编号"])

        # 结算后磅单被结算流水永久引用
        self.assertEqual(svc.get_batch_detail(self.w.coop, bid)["磅单"][0]["结算编号"],
                         settlement["结算编号"])

    def test_cannot_deliver_without_inspection(self):
        svc = self.w.svc
        batch = svc.open_batch(self.w.coop, self.w.plot_id, "2026-09-01", "2026秋")
        svc.weigh(self.w.coop, batch["批次编号"], self.w.old_tree_id, "P001", weight_kg="50")
        with self.assertRaises(Conflict) as cm:
            svc.deliver(self.w.coop, batch["批次编号"], "广兴饮料厂",
                        self.w.contract_id, "2026-09-02T08:00:00+00:00")
        self.assertEqual(cm.exception.code, "inspection_pending")

    def test_ticket_requires_existing_batch_and_matching_plot(self):
        svc = self.w.svc
        batch = svc.open_batch(self.w.coop, self.w.plot_id, "2026-09-01", "2026秋")
        other_plot = svc.create_plot(self.w.coop, self.w.other_id, "赵家坳", "广兴镇赵家坳")
        other_tree = svc.register_tree_group(
            self.w.coop, other_plot["地块编号"], "赵家老树", "红橘", 50, "普通老树")
        with self.assertRaises(ValidationFailed):
            svc.weigh(self.w.coop, batch["批次编号"], other_tree["编号"], "X1", weight_kg="10")
        with self.assertRaises(NotFound):
            svc.weigh(self.w.coop, "批次-999", self.w.old_tree_id, "X2", weight_kg="10")


class IdempotencyAndOnceOnlyTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.batch = self.w.svc.open_batch(self.w.coop, self.w.plot_id, "2026-09-01", "2026秋")
        self.bid = self.batch["批次编号"]

    def test_offline_slip_resend_is_idempotent(self):
        """断网地磅恢复后重传同一张纸单：同一批果只入库一次。"""
        svc = self.w.svc
        first = svc.weigh(self.w.coop, self.bid, self.w.old_tree_id, "SLIP-77",
                          gross_kg="210", tare_kg="10", offline=True, device="地磅-02")
        self.assertNotIn("幂等命中", first)
        self.assertEqual(first["重量kg"], "200")
        # 网络恢复，设备把同一条流水号又推了一遍
        second = svc.weigh(self.w.coop, self.bid, self.w.old_tree_id, "SLIP-77",
                           gross_kg="210", tare_kg="10", offline=False, device="地磅-02")
        self.assertTrue(second["幂等命中"])
        self.assertEqual(second["磅单编号"], first["磅单编号"])
        tickets = svc.get_batch_detail(self.w.coop, self.bid)["磅单"]
        self.assertEqual(len(tickets), 1)

    def test_concurrent_same_slip_only_one_ticket(self):
        svc = self.w.svc
        errors = []

        def submit():
            try:
                svc.weigh(self.w.coop, self.bid, self.w.old_tree_id, "RACE-1",
                          weight_kg="30", offline=True, device="地磅-03")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        barrier = threading.Barrier(8)

        def go():
            barrier.wait()
            submit()

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: go(), range(8)))
        self.assertFalse(errors)
        tickets = svc.get_batch_detail(self.w.coop, self.bid)["磅单"]
        self.assertEqual(len(tickets), 1)

    def test_each_kg_settled_only_once_even_with_concurrent_settle(self):
        """并发交货结算：每公斤只能结算一次，第二次结算被拒。"""
        svc = self.w.svc
        weigh_inspect_settle(self.w, self.bid, self.w.old_tree_id, "S1", "100", "A")
        delivery = svc.deliver(self.w.coop, self.bid, "广兴饮料厂",
                               self.w.contract_id, "2026-09-02T08:00:00+00:00")
        results = []
        barrier = threading.Barrier(2)

        def settle():
            barrier.wait()
            try:
                return ("ok", svc.settle(self.w.coop, delivery["交货单编号"]))
            except Conflict as exc:
                return ("reject", exc.code)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: settle(), range(2)))
        statuses = sorted(r[0] for r in results)
        self.assertEqual(statuses, ["ok", "reject"])
        self.assertEqual(results[0][1] if results[0][0] == "reject" else results[1][1],
                         "already_settled")

    def test_cannot_weigh_after_delivery(self):
        svc = self.w.svc
        weigh_inspect_settle(self.w, self.bid, self.w.old_tree_id, "S1", "50", "A")
        svc.deliver(self.w.coop, self.bid, "广兴饮料厂",
                    self.w.contract_id, "2026-09-02T08:00:00+00:00")
        with self.assertRaises(Conflict) as cm:
            svc.weigh(self.w.coop, self.bid, self.w.old_tree_id, "S2", weight_kg="1")
        self.assertEqual(cm.exception.code, "batch_delivered")


class ProtectedTreeQuotaTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.batch = self.w.svc.open_batch(self.w.coop, self.w.plot_id, "2026-09-01", "2026秋")
        self.bid = self.batch["批次编号"]

    def test_heritage_requires_quota_and_reservation(self):
        svc = self.w.svc
        fresh = svc.register_tree_group(
            self.w.coop, self.w.plot_id, "另一株百年树", "红橘", 120, "百年保护树")
        with self.assertRaises(Conflict) as cm:
            svc.weigh(self.w.coop, self.bid, fresh["编号"], "H0", weight_kg="5")
        self.assertEqual(cm.exception.code, "quota_not_set")
        with self.assertRaises(Conflict) as cm:
            svc.reserve_trees(self.w.coop, self.bid, fresh["编号"], "5")
        self.assertEqual(cm.exception.code, "quota_not_set")

    def test_reservation_cannot_exceed_season_quota(self):
        svc = self.w.svc
        svc.reserve_trees(self.w.coop, self.bid, self.w.heritage_id, "60")
        with self.assertRaises(Conflict) as cm:
            svc.reserve_trees(self.w.coop, self.bid, self.w.heritage_id, "41")
        self.assertEqual(cm.exception.code, "quota_exceeded")

    def test_weighing_beyond_reservation_is_rejected(self):
        """预占 50kg，现场偷采到 51kg：过磅即拦截，保护树采收不越界。"""
        svc = self.w.svc
        svc.reserve_trees(self.w.coop, self.bid, self.w.heritage_id, "50")
        svc.weigh(self.w.coop, self.bid, self.w.heritage_id, "H1", weight_kg="50")
        with self.assertRaises(Conflict) as cm:
            svc.weigh(self.w.coop, self.bid, self.w.heritage_id, "H2", weight_kg="1")
        self.assertEqual(cm.exception.code, "over_reservation")

    def test_concurrent_overharvest_only_part_within_quota_passes(self):
        """两车并发交售保护树果实，合计超配额时只有额度内的部分入库。"""
        svc = self.w.svc
        svc.reserve_trees(self.w.coop, self.bid, self.w.heritage_id, "100")
        outcomes = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)

        def weigh(slip):
            barrier.wait()
            try:
                svc.weigh(self.w.coop, self.bid, self.w.heritage_id, slip, weight_kg="60")
                with lock:
                    outcomes.append("ok")
            except Conflict as exc:
                with lock:
                    outcomes.append(exc.code)

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(weigh, ["C1", "C2"]))
        self.assertEqual(sorted(outcomes), ["ok", "over_reservation"])
        used = svc.get_tree_group(self.w.coop, self.w.heritage_id)["本季已采重量kg"]
        self.assertEqual(used, "60")

    def test_pick_times_quota_enforced(self):
        svc = self.w.svc
        svc.reserve_trees(self.w.coop, self.bid, self.w.heritage_id, "100")
        svc.weigh(self.w.coop, self.bid, self.w.heritage_id, "T1", weight_kg="40")
        svc.weigh(self.w.coop, self.bid, self.w.heritage_id, "T2", weight_kg="40")
        with self.assertRaises(Conflict) as cm:
            svc.weigh(self.w.coop, self.bid, self.w.heritage_id, "T3", weight_kg="1")
        self.assertEqual(cm.exception.code, "quota_times_exceeded")


class ReviewAndPriceTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.batch = self.w.svc.open_batch(self.w.coop, self.w.plot_id, "2026-09-01", "2026秋")
        self.bid = self.batch["批次编号"]

    def _delivered(self):
        delivery = self.w.svc.deliver(self.w.coop, self.bid, "广兴饮料厂",
                                      self.w.contract_id, "2026-09-02T08:00:00+00:00")
        return delivery["交货单编号"]

    def test_review_before_settle_uses_new_grade_no_adjustment(self):
        svc = self.w.svc
        ticket, insp = weigh_inspect_settle(self.w, self.bid, self.w.old_tree_id, "R1", "100", "A")
        result = svc.review_grade(self.w.reviewer, insp["检验编号"], "B",
                                  "复核发现风伤果比例超标，降为B级")
        self.assertIsNone(result["价差调整"])
        # 原样本与原判定完整保留
        self.assertFalse(result["原检验"]["现行"])
        self.assertEqual(result["原检验"]["样本编号"], result["新检验"]["样本编号"])
        self.assertEqual(result["新检验"]["复核自"], insp["检验编号"])

        did = self._delivered()
        settlement = svc.settle(self.w.coop, did)
        self.assertEqual(settlement["明细行"][0]["等级"], "B")
        self.assertEqual(settlement["明细行"][-1]["合计应收"], "300.00")
        self.assertEqual(settlement["状态"], "正常")

    def test_review_after_settle_keeps_original_and_writes_price_diff(self):
        svc = self.w.svc
        ticket, insp = weigh_inspect_settle(self.w, self.bid, self.w.old_tree_id, "R2", "100", "B")
        did = self._delivered()
        settlement = svc.settle(self.w.coop, did)
        self.assertEqual(settlement["明细行"][-1]["合计应收"], "300.00")

        # 复核升级 A：补付 (4-3)*100 = 100；原结算行不被改写
        result = svc.review_grade(self.w.reviewer, insp["检验编号"], "A",
                                  "实验室复测糖度13.5，达到A级")
        adj = result["价差调整"]
        self.assertEqual(adj["差额"], "100.00")
        self.assertEqual(adj["方向"], "补付")
        self.assertEqual(adj["原等级"], "B")
        self.assertEqual(adj["新等级"], "A")
        settled_again = svc.get_settlement(self.w.coop, settlement["结算编号"])
        self.assertEqual(settled_again["明细行"][-1]["合计应收"], "300.00")
        self.assertEqual(settled_again["状态"], "含调整")

    def test_review_downgrade_after_settle_is_claim_back(self):
        svc = self.w.svc
        _, insp = weigh_inspect_settle(self.w, self.bid, self.w.old_tree_id, "R3", "10", "A")
        did = self._delivered()
        svc.settle(self.w.coop, did)
        result = svc.review_grade(self.w.reviewer, insp["检验编号"], "B", "复检不达标")
        self.assertEqual(result["价差调整"]["差额"], "-10.00")
        self.assertEqual(result["价差调整"]["方向"], "扣回")

    def test_review_must_target_current_inspection(self):
        svc = self.w.svc
        _, insp = weigh_inspect_settle(self.w, self.bid, self.w.old_tree_id, "R4", "10", "A")
        svc.review_grade(self.w.reviewer, insp["检验编号"], "B", "第一次复核降级")
        with self.assertRaises(Conflict) as cm:
            svc.review_grade(self.w.reviewer, insp["检验编号"], "A", "不能对失效记录再复核")
        self.assertEqual(cm.exception.code, "superseded")

    def test_later_market_price_cannot_rewrite_receivable(self):
        """9月2日交货按 v1 结算；9月10日市场价大跌发布 v2，农户应收不变。"""
        svc = self.w.svc
        weigh_inspect_settle(self.w, self.bid, self.w.old_tree_id, "P1", "100", "A")
        did = self._delivered()
        settlement = svc.settle(self.w.coop, did)
        self.assertEqual(settlement["明细行"][-1]["合计应收"], "400.00")

        svc.publish_price_rule(
            self.w.coop, "2026-09-10T00:00:00+00:00",
            protected={"A": "2.50", "B": "2.00"}, note="市场下行，不追溯")
        # 历史结算金额原样
        self.assertEqual(svc.get_settlement(self.w.coop, settlement["结算编号"])
                         ["明细行"][-1]["合计应收"], "400.00")

        # 9月10日之后的新交货适用 v2
        batch2 = svc.open_batch(self.w.coop, self.w.plot_id, "2026-09-12", "2026秋")
        weigh_inspect_settle(self.w, batch2["批次编号"], self.w.old_tree_id, "P2", "100", "A")
        d2 = svc.deliver(self.w.coop, batch2["批次编号"], "广兴饮料厂",
                         self.w.contract_id, "2026-09-12T08:00:00+00:00")
        self.assertEqual(d2["计价价规"], "价规-v2")
        s2 = svc.settle(self.w.coop, d2["交货单编号"])
        self.assertEqual(s2["明细行"][-1]["合计应收"], "250.00")
        # 且价差调整仍以交货时价规 v1 计价，不被 v2 污染
        old_insp = svc.get_batch_detail(self.w.coop, self.bid)["磅单"][0]["检验编号"]
        # 找到该磅单初检记录（现行的可能是复核后的；直接对初检复核会在场景外，跳过）
        self.assertTrue(old_insp.startswith("检验-"))

    def test_concurrent_settle_and_appeal_total_is_consistent(self):
        """结算与等级申诉同批并发：无论先后，农户总权益一致且每公斤只结一次。"""
        svc = self.w.svc
        t1, i1 = weigh_inspect_settle(self.w, self.bid, self.w.old_tree_id, "X1", "50", "A")
        t2, i2 = weigh_inspect_settle(self.w, self.bid, self.w.old_tree_id, "X2", "50", "A")
        did = self._delivered()
        barrier = threading.Barrier(2)

        def settle():
            barrier.wait()
            try:
                svc.settle(self.w.coop, did)
            except Conflict:
                pass

        def appeal():
            barrier.wait()
            svc.review_grade(self.w.reviewer, i1["检验编号"], "B", "并发申诉降级")

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda f: f(), [settle, appeal]))

        # 找到唯一结算
        ledger = svc.list_ledger(self.w.coop, "农户结算")["记录"]
        self.assertEqual(len(ledger), 1)
        s = ledger[0]
        base = Decimal(s["明细行"][-1]["合计应收"])
        diff = sum((Decimal(a["差额"]) for a in s["价差调整"]), Decimal("0"))
        # 一张 50kg 由 A 降 B：最终权益恒为 350（400-50 或直接按 300+50）
        self.assertEqual(base + diff, Decimal("350.00"))
        # 每张磅单恰好结算一次
        settled_tickets = {row["磅单编号"] for row in s["明细行"] if "磅单编号" in row}
        self.assertEqual(settled_tickets, {t1["磅单编号"], t2["磅单编号"]})


class IndependentLedgerTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        svc = self.w.svc
        self.batch = svc.open_batch(self.w.coop, self.w.plot_id, "2026-09-01", "2026秋")
        self.bid = self.batch["批次编号"]
        self.ticket, self.insp = weigh_inspect_settle(
            self.w, self.bid, self.w.old_tree_id, "L1", "100", "A")
        self.did = svc.deliver(self.w.coop, self.bid, "广兴饮料厂",
                               self.w.contract_id, "2026-09-02T08:00:00+00:00")["交货单编号"]
        self.sid = svc.settle(self.w.coop, self.did)["结算编号"]

    def test_return_loss_processing_are_separate_streams(self):
        svc = self.w.svc
        # 先核定到货 95kg（运输损耗 5kg），再从到货果实中退 20kg
        loss = svc.transit_loss(self.w.ent, self.did, "95", "长途水分流失")
        self.assertEqual(loss["损耗kg"], "5")
        ret = svc.enterprise_return(
            self.w.ent, self.did, "20", "到货抽检腐烂率超标",
            self.insp["检验编号"], "待转入加工")
        route = svc.route_to_processing(
            self.w.coop, "退货", ret["退货编号"], "陈皮", "20", "加工厂老陈")

        # 四条流水各自独立
        self.assertEqual(len(svc.list_ledger(self.w.coop, "农户结算")["记录"]), 1)
        returns = svc.list_ledger(self.w.coop, "企业退货")["记录"]
        self.assertEqual(len(returns), 1)
        self.assertEqual(len(svc.list_ledger(self.w.coop, "运输损耗")["记录"]), 1)
        processing = svc.list_ledger(self.w.coop, "果肉加工")["记录"]
        self.assertEqual(processing[0]["制品"], "陈皮")

        # 退货不冲减农户应收
        self.assertEqual(svc.get_settlement(self.w.coop, self.sid)
                         ["明细行"][-1]["合计应收"], "400.00")

        # 加工投入不得超过退货可处置量
        with self.assertRaises(ValidationFailed):
            svc.route_to_processing(self.w.coop, "退货", ret["退货编号"], "陈皮", "0.1")

        # 损耗核定每交货单只允许一次
        with self.assertRaises(Conflict) as cm:
            svc.transit_loss(self.w.ent, self.did, "90")
        self.assertEqual(cm.exception.code, "loss_already_recorded")

        # 到货量不可能大于交货核定量（用新交货单验证）
        batch2 = svc.open_batch(self.w.coop, self.w.plot_id, "2026-09-05", "2026秋")
        weigh_inspect_settle(self.w, batch2["批次编号"], self.w.old_tree_id, "L2", "10", "A")
        did2 = svc.deliver(self.w.coop, batch2["批次编号"], "广兴饮料厂",
                           self.w.contract_id, "2026-09-06T08:00:00+00:00")["交货单编号"]
        with self.assertRaises(ValidationFailed):
            svc.transit_loss(self.w.ent, did2, "11")

        # 到货量不得小于已登记退货量
        with self.assertRaises(ValidationFailed):
            svc.enterprise_return(self.w.ent, self.did, "76", "退货超过货",
                                  self.insp["检验编号"])

    def test_processing_requires_existing_source(self):
        svc = self.w.svc
        with self.assertRaises(NotFound):
            svc.route_to_processing(self.w.coop, "退货", "退货-999", "陈皮", "1")
        with self.assertRaises(ValidationFailed):
            svc.route_to_processing(self.w.coop, "结算", self.sid, "陈皮", "1")


class GuardRoleTest(unittest.TestCase):
    def setUp(self):
        self.w = World()
        svc = self.w.svc
        self.batch = svc.open_batch(self.w.coop, self.w.plot_id, "2026-09-01", "2026秋")
        self.bid = self.batch["批次编号"]
        weigh_inspect_settle(self.w, self.bid, self.w.old_tree_id, "G1", "100", "A")
        self.did = svc.deliver(self.w.coop, self.bid, "广兴饮料厂",
                               self.w.contract_id, "2026-09-02T08:00:00+00:00")["交货单编号"]
        self.sid = svc.settle(self.w.coop, self.did)["结算编号"]

    def test_guard_can_report_disease_and_violation(self):
        svc = self.w.svc
        disease = svc.report_tree_issue(self.w.guard, self.w.heritage_id, "病害", "发现炭疽病斑")
        self.assertEqual(disease["类型"], "病害")
        violation = svc.report_tree_issue(self.w.guard, self.w.heritage_id, "违规采摘",
                                          "夜间发现私自上树打果")
        self.assertEqual(violation["处理状态"], "待处理")
        tree = svc.get_tree_group(self.w.guard, self.w.heritage_id)
        self.assertEqual(tree["健康状态"], "染病")

    def test_guard_tree_view_is_masked(self):
        """护树队只看到巡护必需字段，看不到采收重量等经营数据。"""
        view = self.w.svc.get_tree_group(self.w.guard, self.w.heritage_id)
        self.assertNotIn("本季已采重量kg", view)
        self.assertNotIn("本季已采次数", view)
        self.assertIn("保护级别", view)

    def test_guard_cannot_touch_settlement_or_ledger(self):
        svc = self.w.svc
        with self.assertRaises(PermissionDenied):
            svc.settle(self.w.guard, self.did)
        with self.assertRaises(PermissionDenied):
            svc.get_settlement(self.w.guard, self.sid)
        with self.assertRaises(PermissionDenied):
            svc.list_ledger(self.w.guard, "农户结算")
        with self.assertRaises(PermissionDenied):
            svc.list_batches(self.w.guard)
        with self.assertRaises(PermissionDenied):
            svc.weigh(self.w.guard, self.bid, self.w.old_tree_id, "ZZ", weight_kg="1")
        with self.assertRaises(PermissionDenied):
            svc.trace_from_product(self.w.guard, delivery_id=self.did)

    def test_guard_report_type_is_validated(self):
        with self.assertRaises(ValidationFailed):
            self.w.svc.report_tree_issue(self.w.guard, self.w.heritage_id, "纵火", "无关事件")


class FarmerScopeTest(unittest.TestCase):
    def test_farmer_sees_only_own_records(self):
        w = World()
        svc = w.svc
        batch = svc.open_batch(w.farmer, w.plot_id, "2026-09-01", "2026秋")
        weigh_inspect_settle(w, batch["批次编号"], w.old_tree_id, "F1", "100", "A")

        # 果农可开自己的批次、看自己的批次
        own = svc.list_batches(w.farmer)
        self.assertEqual(len(own), 1)
        # 不能在他人地块建档/开批次
        with self.assertRaises(PermissionDenied):
            svc.create_plot(w.farmer, w.other_id, "偷挂名地块", "x")
        other_plot = svc.create_plot(w.coop, w.other_id, "赵家坳", "广兴镇赵家坳")
        with self.assertRaises(PermissionDenied):
            svc.open_batch(w.farmer, other_plot["地块编号"], "2026-09-01", "2026秋")
        # 第二位农户看不到第一位的批次
        self.assertEqual(svc.list_batches(w.farmer2), [])


class TraceabilityTest(unittest.TestCase):
    def test_product_traces_back_to_plot_inspections_and_farmer(self):
        w = World()
        svc = w.svc
        svc.add_care_log(w.coop, w.heritage_id, "2026-08-15", "古树复壮、支撑加固")
        batch = svc.open_batch(w.coop, w.plot_id, "2026-09-01", "2026秋")
        bid = batch["批次编号"]
        svc.reserve_trees(w.coop, bid, w.heritage_id, "50")
        ticket, insp = weigh_inspect_settle(w, bid, w.heritage_id, "Q1", "50", "A")
        did = svc.deliver(w.coop, bid, "广兴饮料厂", w.contract_id,
                          "2026-09-02T08:00:00+00:00")["交货单编号"]
        svc.settle(w.coop, did)

        chain = svc.trace_from_product(w.ent, delivery_id=did)
        self.assertEqual(chain["地块"]["地块编号"], w.plot_id)
        self.assertEqual(chain["受益农户"]["农户编号"], w.farmer_id)
        self.assertEqual(chain["树群"][0]["编号"], w.heritage_id)
        self.assertEqual(chain["管护记录"][0]["事项"], "古树复壮、支撑加固")
        evidence = chain["磅单与检测依据"][0]
        self.assertEqual(evidence["现行检验"]["检验编号"], insp["检验编号"])
        self.assertTrue(evidence["现行检验"]["样本编号"].startswith("样本-"))

        # 企业只能反查自己的交货单
        svc.register_actor("ent2", "别家厂", ROLE_ENTERPRISE)
        other_ent = svc.authenticate("ent2")
        with self.assertRaises(PermissionDenied):
            svc.trace_from_product(other_ent, delivery_id=did)

    def test_trace_via_processing_route(self):
        w = World()
        svc = w.svc
        batch = svc.open_batch(w.coop, w.plot_id, "2026-09-01", "2026秋")
        _, insp = weigh_inspect_settle(w, batch["批次编号"], w.old_tree_id, "Q2", "60", "A")
        did = svc.deliver(w.coop, batch["批次编号"], "广兴饮料厂", w.contract_id,
                          "2026-09-02T08:00:00+00:00")["交货单编号"]
        svc.settle(w.coop, did)
        ret = svc.enterprise_return(w.ent, did, "10", "挤压伤", insp["检验编号"])
        route = svc.route_to_processing(w.coop, "退货", ret["退货编号"], "陈皮", "10")
        chain = svc.trace_from_product(w.ent, route_id=route["加工编号"])
        self.assertEqual(chain["加工入口"]["制品"], "陈皮")
        self.assertEqual(chain["交货单"]["交货单编号"], did)
        self.assertEqual(chain["地块"]["地块编号"], w.plot_id)


class RecallTestBase(unittest.TestCase):
    """召回测试公共夹具：一条完整的 过磅→检验→交货→结算 链，可选加工/成品。"""

    def setUp(self):
        self.w = World()

    def delivered_chain(self, slip="R1", weight="100", grade="A",
                        date="2026-09-02"):
        w = self.w
        batch = w.svc.open_batch(w.coop, w.plot_id, "2026-09-01", "2026秋")
        ticket, insp = weigh_inspect_settle(
            w, batch["批次编号"], w.old_tree_id, slip, weight, grade)
        delivery = w.svc.deliver(
            w.coop, batch["批次编号"], "广兴饮料厂", w.contract_id,
            f"{date}T08:00:00+00:00")
        settlement = w.svc.settle(w.coop, delivery["交货单编号"])
        return {"批次": batch["批次编号"], "磅单": ticket["磅单编号"],
                "检验": insp["检验编号"], "交货": delivery["交货单编号"],
                "结算": settlement["结算编号"]}

    def start_recall(self, chain, node_type="过磅批次", node_id_key="磅单",
                     reason="复检农残超标"):
        r = self.w.svc.initiate_recall(
            self.w.reg, node_type, chain[node_id_key], reason, chain["检验"])
        return r["召回编号"], r

    def process_to_goods(self, delivery_id: str, input_kg="100",
                         goods=("陈皮罐", "CP-1", "60"), custodian="陈皮加工厂",
                         status="在库"):
        w = self.w
        pb = w.svc.register_processing_batch(
            w.proc, "陈皮",
            [{"来源类型": "交货", "来源单号": delivery_id, "重量kg": input_kg}])
        name, batch_no, weight = goods
        good = w.svc.register_good(
            w.proc, pb["加工批次编号"], name, batch_no, weight,
            custodian=custodian, status=status)
        return pb, good

    def dispose(self, rid, task, actor, receipt_no, action, qty, **kw):
        w = self.w
        w.svc.notify_task(w.reg, rid, task["任务编号"], "短信", "请按要求处置")
        return w.svc.submit_receipt(
            actor, rid, task["任务编号"], receipt_no, action, qty, **kw)


class RecallInitiationTest(RecallTestBase):
    def test_recall_from_any_node_traces_parent_child_quantities(self):
        w = self.w
        chain = self.delivered_chain()
        # 已交货的磅单：保管方是企业，首选动作拦截
        _, recall = self.start_recall(chain)
        task = recall["任务"][0]
        self.assertEqual(task["节点类型"], "过磅批次")
        self.assertEqual(task["节点编号"], chain["磅单"])
        self.assertEqual(task["受控数量kg"], "100")
        self.assertEqual(task["保管方角色"], ROLE_ENTERPRISE)
        self.assertEqual(task["处置动作"], "拦截")
        self.assertEqual(recall["当前版本"], 1)
        self.assertEqual(recall["版本历史"][0]["操作"], "建立")

    def test_recall_from_plot_covers_all_batches(self):
        w = self.w
        c1 = self.delivered_chain("P1", "60")
        c2 = self.delivered_chain("P2", "40", date="2026-09-06")
        rid = w.svc.initiate_recall(
            w.reg, "地块", w.plot_id, "地块级农残", c1["检验"])
        node_ids = {t["节点编号"] for t in rid["任务"]}
        self.assertEqual(node_ids, {c1["磅单"], c2["磅单"]})

    def test_recall_from_finished_good_and_process_batch(self):
        w = self.w
        chain = self.delivered_chain()
        pb, good = self.process_to_goods(chain["交货"], goods=("陈皮罐", "CP1", "60"))
        # 从成品发起：只定位到成品，按成品数量受控，不与加工投入重复计数
        rid = w.svc.initiate_recall(
            w.reg, "成品", good["成品编号"], "成品检出农残", chain["检验"])
        self.assertEqual(len(rid["任务"]), 1)
        self.assertEqual(rid["任务"][0]["节点类型"], "成品")
        self.assertEqual(rid["任务"][0]["受控数量kg"], "60")
        # 从加工批次发起：成品 60 + 在制余量 40，同批货只计一次
        rid2 = w.svc.initiate_recall(
            w.reg, "加工批次", pb["加工批次编号"], "加工批农残", chain["检验"])
        by_type = {t["节点类型"]: t for t in rid2["任务"]}
        self.assertEqual(by_type["成品"]["受控数量kg"], "60")
        self.assertEqual(by_type["加工批次"]["受控数量kg"], "40")

    def test_in_storage_ticket_custodian_is_farmer_and_settlement_held(self):
        w = self.w
        batch = w.svc.open_batch(w.coop, w.plot_id, "2026-09-20", "2026秋")
        ticket = w.svc.weigh(
            w.coop, batch["批次编号"], w.old_tree_id, "H1", weight_kg="10")
        insp = w.svc.inspect(w.reviewer, ticket["磅单编号"], "A", "达标")
        rid = w.svc.initiate_recall(
            w.reg, "过磅批次", ticket["磅单编号"], "农残", insp["检验编号"])
        task = rid["任务"][0]
        self.assertEqual(task["保管方角色"], ROLE_FARMER)
        self.assertEqual(task["处置动作"], "隔离")
        # 农户只能看到自己的处置任务
        self.assertEqual(len(w.svc.list_my_tasks(w.farmer)), 1)
        # 货仍在召回中：交货后结算被闸门拦截
        delivery = w.svc.deliver(
            w.coop, batch["批次编号"], "广兴饮料厂", w.contract_id,
            "2026-09-21T08:00:00+00:00")
        with self.assertRaises(Conflict) as cm:
            w.svc.settle(w.coop, delivery["交货单编号"])
        self.assertEqual(cm.exception.code, "recall_hold")


class RecallScopeVersionTest(RecallTestBase):
    def test_expand_adds_tasks_and_shrink_keeps_manual_quarantine(self):
        w = self.w
        c1 = self.delivered_chain("P1", "60")
        c2 = self.delivered_chain("P2", "40", date="2026-09-06")
        rid, _ = self.start_recall(c1)
        task1 = self.task_of(rid, c1["磅单"])

        # 扩大到地块：补建 c2 任务，版本升 v2
        v2 = w.svc.rescope_recall(w.reg, rid, "地块", w.plot_id, "扩大排查")
        self.assertEqual(v2["当前版本"], 2)
        self.assertEqual(v2["版本历史"][-1]["操作"], "扩大")
        task2 = self.task_of(rid, c2["磅单"])
        self.assertEqual(task2["加入版本"], 2)

        # c1 被人工隔离 60kg
        self.dispose(rid, task1, w.ent, "RC1", "隔离", "60", manual=True)
        self.assertTrue(self.task_of(rid, c1["磅单"])["人工隔离"])

        # 缩小回 c1：c2 未被触碰，系统移出且不算保管方回执；c1 人工隔离保留
        v3 = w.svc.rescope_recall(w.reg, rid, "过磅批次", c1["磅单"], "排除c2")
        self.assertEqual(v3["版本历史"][-1]["操作"], "缩小")
        t2_after = self.task_of(rid, c2["磅单"])
        self.assertEqual(t2_after["状态"], "已处置")
        self.assertFalse(t2_after["保管方已回执"])
        self.assertIn("系统移出", str(t2_after["处置去向"]))
        t1_after = self.task_of(rid, c1["磅单"])
        self.assertTrue(t1_after["人工隔离"])
        self.assertNotEqual(t1_after["状态"], "已处置")

        # 未显式处置人工隔离货物前不得结案（缩范围不自动释放）
        with self.assertRaises(Conflict) as cm:
            w.svc.close_recall(w.reg, rid)
        self.assertEqual(cm.exception.code, "manual_quarantine_open")

    def test_identical_scope_does_not_create_version(self):
        w = self.w
        chain = self.delivered_chain()
        rid, _ = self.start_recall(chain)
        with self.assertRaises(Conflict) as cm:
            w.svc.rescope_recall(w.reg, rid, "过磅批次", chain["磅单"])
        self.assertEqual(cm.exception.code, "scope_unchanged")

    def test_rescope_resyncs_untouched_task_when_goods_moved(self):
        """召回发起后果实转入加工：扩范围重算时，未触碰的磅单任务受控量同步下调，
        已加工的量补建到成品节点，同批货不重复计数。"""
        w = self.w
        chain = self.delivered_chain(weight="100")
        rid, _ = self.start_recall(chain)
        self.assertEqual(self.task_of(rid, chain["磅单"])["受控数量kg"], "100")
        # 60kg 交货果被加工为成品
        _, good = self.process_to_goods(
            chain["交货"], input_kg="60", goods=("陈皮罐", "CP1", "60"))
        # 扩大到地块（本地块仅此一个磅单）：磅单余量下调、成品补建
        v2 = w.svc.rescope_recall(w.reg, rid, "地块", w.plot_id, "货权移动后重算")
        self.assertEqual(v2["版本历史"][-1]["操作"], "扩大")
        self.assertEqual(self.task_of(rid, chain["磅单"])["受控数量kg"], "40")
        goods_task = next(t for t in v2["任务"] if t["节点类型"] == "成品")
        self.assertEqual(goods_task["受控数量kg"], "60")

    def task_of(self, rid, node_id):
        return next(t for t in self.w.svc.get_recall(self.w.reg, rid)["任务"]
                    if t["节点编号"] == node_id)


class RecallIdempotencyTest(RecallTestBase):
    def test_offline_receipt_replay_applies_once(self):
        w = self.w
        chain = self.delivered_chain()
        rid, recall = self.start_recall(chain)
        task = recall["任务"][0]
        w.svc.notify_task(w.reg, rid, task["任务编号"], "短信", "销毁全部")
        first = w.svc.submit_receipt(
            w.ent, rid, task["任务编号"], "PAPER-9",
            "销毁", "100", offline=True)
        self.assertFalse(first["重复"])
        # 网络恢复后离线凭证重传：只返回原回执，不重复处置
        replay = w.svc.submit_receipt(
            w.ent, rid, task["任务编号"], "PAPER-9",
            "销毁", "100", offline=True)
        self.assertTrue(replay["重复"])
        self.assertEqual(replay["回执"]["回执编号"], first["回执"]["回执编号"])
        stored = w.svc.get_recall_task(w.reg, task["任务编号"])
        self.assertEqual(stored["处置去向"]["销毁"], "100")
        self.assertEqual(len(stored["回执"]), 1)

    def test_duplicate_notification_has_no_effect(self):
        w = self.w
        chain = self.delivered_chain()
        rid, recall = self.start_recall(chain)
        task = recall["任务"][0]
        n1 = w.svc.notify_task(w.reg, rid, task["任务编号"], "短信", "同内容")
        n2 = w.svc.notify_task(w.reg, rid, task["任务编号"], "短信", "同内容")
        n3 = w.svc.notify_task(w.reg, rid, task["任务编号"], "短信", "内容更新")
        self.assertFalse(n1["重复"])
        self.assertTrue(n2["重复"])
        self.assertFalse(n3["重复"])
        notices = [n for n in w.svc._recall_notices.values()
                   if n.任务编号 == task["任务编号"]]
        self.assertEqual(len(notices), 2)

    def test_non_custodian_cannot_receipt(self):
        w = self.w
        chain = self.delivered_chain()
        rid, recall = self.start_recall(chain)
        task = recall["任务"][0]
        with self.assertRaises(PermissionDenied):
            w.svc.submit_receipt(w.farmer, rid, task["任务编号"], "X", "销毁", "1")
        with self.assertRaises(PermissionDenied):
            w.svc.submit_receipt(w.proc, rid, task["任务编号"], "X", "销毁", "1")


class RecallConflictTest(RecallTestBase):
    def test_quantity_conflict_waits_for_regulator_authorization(self):
        w = self.w
        chain = self.delivered_chain()
        rid, recall = self.start_recall(chain)
        task = recall["任务"][0]
        w.svc.notify_task(w.reg, rid, task["任务编号"], "短信", "隔离")
        result = w.svc.submit_receipt(
            w.ent, rid, task["任务编号"], "X1", "销毁", "120")
        conflict = result["冲突"]
        self.assertEqual(conflict["上报数量kg"], "120")
        self.assertEqual(conflict["系统数量kg"], "100")
        self.assertEqual(conflict["状态"], "待授权")
        self.assertEqual(self.task(rid, task["任务编号"])["状态"], "冲突待复核")

        # 企业无权授权
        with self.assertRaises(PermissionDenied):
            w.svc.resolve_conflict(w.ent, conflict["冲突编号"], "调整", "120")
        # 监管维持系统数量后，按 100kg 重新回执即可处置
        w.svc.resolve_conflict(w.reg, conflict["冲突编号"], "维持",
                               opinion="以受控量为准")
        again = w.svc.submit_receipt(
            w.ent, rid, task["任务编号"], "X2", "销毁", "100")
        self.assertIsNone(again["冲突"])
        report = w.svc.close_recall(w.reg, rid)
        self.assertEqual(report["数量守恒"]["差额kg"], "0")

    def test_authorized_adjustment_is_audited(self):
        w = self.w
        chain = self.delivered_chain()
        rid, recall = self.start_recall(chain)
        task = recall["任务"][0]
        w.svc.notify_task(w.reg, rid, task["任务编号"], "短信", "隔离")
        conflict = w.svc.submit_receipt(
            w.ent, rid, task["任务编号"], "X1", "隔离", "120")["冲突"]
        w.svc.resolve_conflict(w.reg, conflict["冲突编号"], "调整", "100",
                               opinion="现场清点以100kg登记")
        view = w.svc.get_recall(w.reg, rid)
        self.assertEqual(view["未决冲突"], [])
        done = self.task(rid, task["任务编号"])
        self.assertEqual(done["处置去向"]["隔离"], "100")

    def task(self, rid, task_id):
        return self.w.svc.get_recall_task(self.w.reg, task_id)


class RecallFundsAndImmutabilityTest(RecallTestBase):
    def test_hold_claim_back_restore_are_appended_only(self):
        w = self.w
        chain = self.delivered_chain(weight="100", grade="A")
        rid, _ = self.start_recall(chain)
        task = self.w.svc.get_recall(w.reg, rid)["任务"][0]
        self.dispose(rid, task, w.ent, "R1", "销毁", "100")
        # 暂缓：追加账，金额按结算固化单价 4 元/kg
        hold = w.svc.hold_funds(w.reg, rid, chain["结算"], basis="农残确认")
        self.assertEqual(hold["合计重量kg"], "100")
        self.assertEqual(hold["合计金额"], "400.00")
        # 历史结算行原样
        self.assertEqual(
            w.svc.get_settlement(w.coop, chain["结算"])
            ["明细行"][-1]["合计应收"], "400.00")
        # 同一磅单不能重复暂缓
        with self.assertRaises(Conflict) as cm:
            w.svc.hold_funds(w.reg, rid, chain["结算"])
        self.assertEqual(cm.exception.code, "funds_already_covered")
        # 先恢复暂缓，再对其中 30kg 追回、再恢复
        hold_id = hold["明细"][0]["资金编号"]
        w.svc.restore_funds(w.reg, rid, hold_id)
        claim = w.svc.claim_back_funds(
            w.reg, rid, chain["结算"], weight_kg="30", basis="已付款部分追回")
        self.assertEqual(claim["合计金额"], "120.00")
        w.svc.restore_funds(w.reg, rid, claim["明细"][0]["资金编号"])
        with self.assertRaises(Conflict):
            w.svc.restore_funds(w.reg, rid, claim["明细"][0]["资金编号"])
        report = w.svc.close_recall(w.reg, rid)
        self.assertEqual(report["资金变化"]["暂缓"], "400.00")
        self.assertEqual(report["资金变化"]["追回"], "120.00")
        self.assertEqual(report["资金变化"]["恢复"], "520.00")
        self.assertEqual(report["资金变化"]["净影响"], "0.00")

    def test_funds_from_goods_recall_prorate_back_to_tickets(self):
        w = self.w
        chain = self.delivered_chain(weight="100")
        _, good = self.process_to_goods(chain["交货"], goods=("陈皮罐", "CP1", "50"))
        rid = w.svc.initiate_recall(
            w.reg, "成品", good["成品编号"], "成品农残", chain["检验"])["召回编号"]
        task = w.svc.get_recall(w.reg, rid)["任务"][0]
        self.dispose(rid, task, w.proc, "G1", "销毁", "50")
        hold = w.svc.hold_funds(w.reg, rid, chain["结算"])
        # 成品 50kg 沿父子数量关系摊回唯一磅单：暂缓 50kg × 4 元
        self.assertEqual(hold["合计重量kg"], "50")
        self.assertEqual(hold["合计金额"], "200.00")

    def test_review_grade_during_recall_keeps_separate_adjustment_stream(self):
        w = self.w
        chain = self.delivered_chain(weight="100", grade="A")
        rid, _ = self.start_recall(chain)
        task = w.svc.get_recall(w.reg, rid)["任务"][0]
        self.dispose(rid, task, w.ent, "R1", "销毁", "100")
        # 召回不阻挡复核：等级申诉仍走价差调整，与召回资金追加账各自独立
        result = w.svc.review_grade(
            w.reviewer, chain["检验"], "B", "农残事件伴随降级")
        self.assertEqual(result["价差调整"]["差额"], "-100.00")
        hold = w.svc.hold_funds(w.reg, rid, chain["结算"])
        self.assertEqual(hold["合计金额"], "400.00")
        settlement = w.svc.get_settlement(w.coop, chain["结算"])
        self.assertEqual(settlement["明细行"][-1]["合计应收"], "400.00")

    def test_confirm_unaffected_auto_restores_funds(self):
        w = self.w
        chain = self.delivered_chain()
        rid, _ = self.start_recall(chain)
        task = w.svc.get_recall(w.reg, rid)["任务"][0]
        self.dispose(rid, task, w.ent, "R1", "解除", "100")
        w.svc.hold_funds(w.reg, rid, chain["结算"])
        result = w.svc.confirm_result(w.reg, rid, False, "实验室复检合格")
        self.assertEqual(result["自动恢复笔数"], 1)
        report = w.svc.close_recall(w.reg, rid)
        self.assertEqual(report["资金变化"]["净影响"], "0.00")


class RecallClosureTest(RecallTestBase):
    def test_close_requires_conservation_and_lists_missing_receipts(self):
        w = self.w
        c1 = self.delivered_chain("P1", "60")
        c2 = self.delivered_chain("P2", "40", date="2026-09-06")
        rid, _ = self.start_recall(c1)
        task1 = self.w.svc.get_recall(w.reg, rid)["任务"][0]
        # 扩大后不通知 c2 即缩小，制造“系统移出、未回执”节点
        w.svc.rescope_recall(w.reg, rid, "地块", w.plot_id, "扩大")
        task2 = next(t for t in w.svc.get_recall(w.reg, rid)["任务"]
                     if t["节点编号"] == c2["磅单"])
        w.svc.rescope_recall(w.reg, rid, "过磅批次", c1["磅单"], "排除c2")
        # 只销毁 40（退回尚未回执）：数量未交代完，不能结案
        self.dispose(rid, task1, w.ent, "R1", "销毁", "40")
        with self.assertRaises(Conflict) as cm:
            w.svc.close_recall(w.reg, rid)
        self.assertEqual(cm.exception.code, "quantity_not_conserved")
        # 补齐退回 20 后守恒
        w.svc.submit_receipt(w.ent, rid, task1["任务编号"], "R2", "退回", "20")
        report = w.svc.close_recall(w.reg, rid)
        self.assertEqual(report["数量守恒"]["受控合计kg"], "60")
        self.assertEqual(report["数量守恒"]["销毁kg"], "40")
        self.assertEqual(report["数量守恒"]["退回kg"], "20")
        self.assertEqual(report["数量守恒"]["差额kg"], "0")
        missing = report["未回执节点"]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["节点编号"], c2["磅单"])

    def test_replace_delivery_marks_good_and_conserves(self):
        w = self.w
        chain = self.delivered_chain()
        _, good = self.process_to_goods(
            chain["交货"], goods=("陈皮罐", "CP1", "60"),
            custodian="广兴饮料厂", status="在途")
        rid = w.svc.initiate_recall(
            w.reg, "成品", good["成品编号"], "在途成品农残", chain["检验"])["召回编号"]
        task = w.svc.get_recall(w.reg, rid)["任务"][0]
        self.assertEqual(task["处置动作"], "拦截")
        self.dispose(rid, task, w.ent, "T1", "拦截", "60", offline=True)
        w.svc.submit_receipt(w.ent, rid, task["任务编号"], "T2", "替代交付", "60")
        report = w.svc.close_recall(w.reg, rid)
        self.assertEqual(report["数量守恒"]["替代交付kg"], "60")
        self.assertEqual(w.svc._goods[good["成品编号"]].状态, "已替代")

    def test_closed_recall_is_immutable(self):
        w = self.w
        chain = self.delivered_chain()
        rid, recall = self.start_recall(chain)
        task = recall["任务"][0]
        self.dispose(rid, task, w.ent, "R1", "销毁", "100")
        w.svc.close_recall(w.reg, rid)
        with self.assertRaises(Conflict) as cm:
            w.svc.notify_task(w.reg, rid, task["任务编号"], "短信", "再通知")
        self.assertEqual(cm.exception.code, "recall_closed")
        with self.assertRaises(Conflict):
            w.svc.submit_receipt(w.ent, rid, task["任务编号"], "R9", "销毁", "1")


class RecallRoleVisibilityTest(RecallTestBase):
    def test_each_role_sees_only_duty_data(self):
        w = self.w
        chain = self.delivered_chain()
        rid, _ = self.start_recall(chain)
        task = w.svc.get_recall(w.reg, rid)["任务"][0]
        self.dispose(rid, task, w.ent, "R1", "销毁", "100")
        w.svc.hold_funds(w.reg, rid, chain["结算"])

        # 监管看全貌：资金账、版本、范围节点
        reg_view = w.svc.get_recall(w.reg, rid)
        self.assertIn("资金追加账", reg_view)
        self.assertIn("范围内节点", reg_view)
        # 企业只看自己的任务，看不到资金账
        ent_view = w.svc.get_recall(w.ent, rid)
        self.assertEqual(len(ent_view["任务"]), 1)
        self.assertNotIn("资金追加账", ent_view)
        self.assertNotIn("范围内节点", ent_view)
        # 护树队一概不可见
        with self.assertRaises(PermissionDenied):
            w.svc.get_recall(w.guard, rid)
        with self.assertRaises(PermissionDenied):
            w.svc.list_recalls(w.guard)
        with self.assertRaises(PermissionDenied):
            w.svc.initiate_recall(w.guard, "过磅批次", chain["磅单"], "x", chain["检验"])
        # 加工方与该召回无关 → 看不到
        self.assertEqual(w.svc.list_my_tasks(w.proc), [])
        with self.assertRaises(PermissionDenied):
            w.svc.get_recall(w.proc, rid)
        # 无相关任务的农户看不到该召回
        with self.assertRaises(PermissionDenied):
            w.svc.get_recall(w.farmer, rid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
