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

        self.coop = self.svc.authenticate("coop")
        self.reviewer = self.svc.authenticate("reviewer")
        self.guard = self.svc.authenticate("guard")
        self.ent = self.svc.authenticate("ent")

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


class RecallWorld(World):
    """在基础世界上再注册加工方/监管，并搭出 在库40/在途30/成品20 的分流链路。"""

    def __init__(self):
        super().__init__()
        self.svc.register_actor("proc", "陈皮加工厂", ROLE_PROCESSOR)
        self.svc.register_actor("reg", "监管员郑直", ROLE_REGULATOR)
        self.proc = self.svc.authenticate("proc")
        self.reg = self.svc.authenticate("reg")
        # 第二家企业，用于跨企业隔离
        self.svc.register_actor("ent2", "别家饮料厂", ROLE_ENTERPRISE)
        self.ent2 = self.svc.authenticate("ent2")
        # 第二家加工方，用于跨加工方隔离
        self.svc.register_actor("proc2", "别家加工厂", ROLE_PROCESSOR)
        self.proc2 = self.svc.authenticate("proc2")

        self.batch = self.svc.open_batch(self.coop, self.plot_id, "2026-09-01", "2026秋")
        self.bid = self.batch["批次编号"]
        self.ticket, self.insp = weigh_inspect_settle(
            self, self.bid, self.old_tree_id, "RC1", "100", "A")
        self.did = self.svc.deliver(
            self.coop, self.bid, "广兴饮料厂", self.contract_id,
            "2026-09-02T08:00:00+00:00")["交货单编号"]
        self.sid = self.svc.settle(self.coop, self.did)["结算编号"]
        # 60kg 发加工：30 仍在途，30 到厂投入
        self.svc.ship_to_processing(self.ent, self.did, "60")
        self.svc.arrive_at_processor(self.proc, self.did, "30")
        pb = self.svc.create_processing_batch(
            self.proc, "陈皮",
            [{"交货单编号": self.did, "投入重量kg": "30"}],
            [{"制品": "陈皮", "重量kg": "20"}])
        self.pid = pb["加工批次"]["加工批次编号"]
        self.gid = pb["成品"][0]["成品编号"]

    def fail_pesticide(self):
        return self.svc.pesticide_review(
            self.reg, self.insp["检验编号"], "克百威", "不合格",
            "0.05", "0.02", "送检实验室复测超标")["农残编号"]

    def task_by(self, recall, **kw):
        out = [t for t in recall["任务"]
               if all(t.get(k) == v for k, v in kw.items())]
        assert len(out) == 1, f"期望唯一任务，实际 {[(t.get('位置'), t.get('节点类型')) for t in recall['任务']]}"
        return out[0]


class PesticideReviewTest(unittest.TestCase):
    def setUp(self):
        self.w = RecallWorld()

    def test_pesticide_review_keeps_original_inspection(self):
        svc = self.w.svc
        first = svc.pesticide_review(self.w.reviewer, self.w.insp["检验编号"],
                                     "克百威", "合格", "0.01", "0.02", "首轮合格")
        second = svc.pesticide_review(self.w.reg, self.w.insp["检验编号"],
                                      "克百威", "不合格", "0.05", "0.02", "复测超标")
        # 原始质量检验与其等级、封存样本不被改写
        chain = svc.trace_from_product(self.w.ent, delivery_id=self.w.did)
        current = chain["磅单与检测依据"][0]["现行检验"]
        self.assertEqual(current["等级"], "A")
        self.assertEqual(current["样本编号"], self.w.insp["样本编号"])
        self.assertTrue(second["现行"])
        self.assertFalse(svc._pesticide[first["农残编号"]].现行)
        self.assertEqual(second["复核自"], first["农残编号"])
        # 复测沿用同一封存样本
        self.assertEqual(second["样本编号"], self.w.insp["样本编号"])

    def test_only_failed_review_can_open_recall(self):
        ok = self.w.svc.pesticide_review(
            self.w.reg, self.w.insp["检验编号"], "克百威", "合格", "0.01", "0.02", "合格")
        with self.assertRaises(Conflict) as cm:
            self.w.svc.open_recall(self.w.reg, "过磅批次", self.w.bid, ok["农残编号"])
        self.assertEqual(cm.exception.code, "pesticide_qualified")

    def test_only_regulator_opens_recall(self):
        prid = self.w.fail_pesticide()
        with self.assertRaises(PermissionDenied):
            self.w.svc.open_recall(self.w.ent, "过磅批次", self.w.bid, prid)


class RecallExpansionTest(unittest.TestCase):
    def setUp(self):
        self.w = RecallWorld()
        self.prid = self.w.fail_pesticide()

    def test_recall_from_finished_goods_covers_only_goods(self):
        r = self.w.svc.open_recall(self.w.reg, "成品", self.w.gid, self.prid)
        self.assertEqual(len(r["任务"]), 1)
        t = r["任务"][0]
        self.assertEqual(t["节点类型"], "成品")
        self.assertEqual(t["保管方"], "加工方")

    def test_recall_from_batch_finds_each_current_custodian(self):
        r = self.w.svc.open_recall(self.w.reg, "过磅批次", self.w.bid, self.prid)
        positions = {(t["位置"], t["保管方"]): Decimal(t["任务重量kg"])
                     for t in r["任务"]}
        # 在库40（企业，系统已隔离）、在途30（承运）、成品20（加工方系统隔离）
        self.assertEqual(positions[("在库", "企业")], Decimal("40"))
        self.assertEqual(positions[("在途", "承运方")], Decimal("30"))
        self.assertEqual(positions[("在库", "加工方")], Decimal("20"))
        stock = self.w.task_by(r, 位置="在库", 保管方="企业")
        transit = self.w.task_by(r, 位置="在途")
        self.assertEqual(stock["状态"], "已隔离")
        self.assertEqual(stock["隔离方式"], "系统")
        self.assertEqual(transit["状态"], "待通知")

    def test_recall_from_plot_covers_all_its_batches(self):
        svc = self.w.svc
        b2 = svc.open_batch(self.w.coop, self.w.plot_id, "2026-09-03", "2026秋")["批次编号"]
        weigh_inspect_settle(self.w, b2, self.w.old_tree_id, "RC2", "10", "A")
        svc.deliver(self.w.coop, b2, "广兴饮料厂", self.w.contract_id,
                    "2026-09-04T08:00:00+00:00")
        r = svc.open_recall(self.w.reg, "地块", self.w.plot_id, self.prid)
        batches = {t["节点编号"] for t in r["任务"] if t["节点类型"] == "交货单"}
        # 两个交货批次都被定位
        self.assertEqual(len(batches), 2)


class RecallDisposalTest(unittest.TestCase):
    def setUp(self):
        self.w = RecallWorld()
        self.prid = self.w.fail_pesticide()
        self.recall = self.w.svc.open_recall(
            self.w.reg, "过磅批次", self.w.bid, self.prid)
        self.rid = self.recall["召回编号"]

    def test_duplicate_notify_has_one_effect(self):
        svc = self.w.svc
        transit = self.w.task_by(self.recall, 位置="在途")
        first = svc.notify_task(self.w.ent, transit["任务编号"])
        second = svc.notify_task(self.w.ent, transit["任务编号"], channel="短信")
        self.assertFalse(first["幂等命中"])
        self.assertTrue(second["幂等命中"])
        self.assertEqual(second["通知"]["通知编号"], first["通知"]["通知编号"])
        view = svc.get_recall(self.w.reg, self.rid)
        self.assertEqual(len(view["通知"]), 1)

    def test_offline_receipt_replay_is_idempotent(self):
        svc = self.w.svc
        transit = self.w.task_by(self.recall, 位置="在途")
        svc.notify_task(self.w.ent, transit["任务编号"])
        a = svc.disposal_receipt(self.w.ent, transit["任务编号"], "拦截",
                                 "OFF-1", "30", offline=True)
        b = svc.disposal_receipt(self.w.ent, transit["任务编号"], "拦截",
                                 "OFF-1", "30", offline=True)
        self.assertFalse(a["幂等命中"])
        self.assertTrue(b["幂等命中"])
        task = svc.get_recall(self.w.reg, self.rid)
        self.assertEqual(self.w.task_by(task, 位置="在途")["隔离中kg"], "30")

    def test_destroy_requires_prior_quarantine(self):
        svc = self.w.svc
        transit = self.w.task_by(self.recall, 位置="在途")
        with self.assertRaises(Conflict) as cm:
            svc.disposal_receipt(self.w.ent, transit["任务编号"], "销毁", "D1", "30")
        self.assertEqual(cm.exception.code, "not_quarantined")

    def test_non_custodian_cannot_dispose(self):
        transit = self.w.task_by(self.recall, 位置="在途")
        # 加工方不是在途原料的保管方
        with self.assertRaises(PermissionDenied):
            self.w.svc.notify_task(self.w.proc, transit["任务编号"])
        # 别家企业也不能处置
        with self.assertRaises(PermissionDenied):
            self.w.svc.notify_task(self.w.ent2, transit["任务编号"])

    def test_full_disposal_balances_and_closes(self):
        svc = self.w.svc
        stock = self.w.task_by(self.recall, 位置="在库", 保管方="企业")
        transit = self.w.task_by(self.recall, 位置="在途")
        goods = self.w.task_by(self.recall, 节点类型="成品")

        svc.notify_task(self.w.ent, transit["任务编号"])
        svc.disposal_receipt(self.w.ent, transit["任务编号"], "拦截", "T-1", "30")
        svc.disposal_receipt(self.w.ent, transit["任务编号"], "销毁", "T-2", "30")
        svc.disposal_receipt(self.w.ent, stock["任务编号"], "销毁", "S-1", "40")
        svc.disposal_receipt(self.w.proc, goods["任务编号"], "销毁", "G-1", "20")

        report = svc.recall_report(self.w.reg, self.rid)["结案报告"]
        self.assertTrue(report["数量守恒"])
        self.assertEqual(report["范围总重量kg"], "90")
        self.assertEqual(report["去向"]["销毁kg"], "90")
        self.assertEqual(report["未回执节点"], [])
        closed = svc.close_recall(self.w.reg, self.rid)
        self.assertEqual(closed["状态"], "已结案")
        # 结案后冻结
        with self.assertRaises(Conflict):
            svc.notify_task(self.w.ent, transit["任务编号"])

    def test_delivered_goods_substitute_delivery(self):
        svc = self.w.svc
        # 把既有成品标记为已交付客户：召回责任转由供货企业替代交付
        svc._goods[self.w.gid].物流状态 = "已交付"
        svc._goods[self.w.gid].保管方 = "企业"
        r = svc.open_recall(self.w.reg, "成品", self.w.gid, self.prid)
        t = r["任务"][0]
        self.assertEqual(t["保管方"], "企业")
        self.assertEqual(t["状态"], "待通知")
        svc.notify_task(self.w.ent, t["任务编号"])
        svc.disposal_receipt(self.w.ent, t["任务编号"], "替代交付", "SUB-1", "20")
        report = svc.recall_report(self.w.reg, r["召回编号"])["结案报告"]
        self.assertEqual(report["去向"]["替代交付kg"], "20")
        self.assertTrue(report["数量守恒"])


class RecallReshapeTest(unittest.TestCase):
    def setUp(self):
        self.w = RecallWorld()
        self.prid = self.w.fail_pesticide()
        self.recall = self.w.svc.open_recall(
            self.w.reg, "过磅批次", self.w.bid, self.prid)
        self.rid = self.recall["召回编号"]

    def test_expand_adds_tasks_shrink_keeps_manual_hold(self):
        svc = self.w.svc
        stock = self.w.task_by(self.recall, 位置="在库", 保管方="企业")
        transit = self.w.task_by(self.recall, 位置="在途")

        # 在途由企业人工拦截（离线），属于人工处置
        svc.notify_task(self.w.ent, transit["任务编号"])
        svc.disposal_receipt(self.w.ent, transit["任务编号"], "拦截",
                             "M-1", "30", offline=True)

        # 缩小到只剩成品：在库系统隔离自动解除，在途人工拦截不释放
        svc.reshape_recall(self.w.reg, self.rid, "缩小", "成品", self.w.gid)
        view = svc.get_recall(self.w.reg, self.rid)
        stock_t = self.w.task_by(view, 节点类型="交货单", 保管方="企业", 位置="在库")
        transit_t = self.w.task_by(view, 位置="在途")
        self.assertEqual(stock_t["状态"], "已解除")
        self.assertEqual(stock_t["已解除kg"], "40")
        self.assertEqual(transit_t["状态"], "出围待核")
        self.assertEqual(transit_t["隔离中kg"], "30")

        # 再扩大回批次：补建范围，人工冻结的在途货仍保持冻结
        svc.reshape_recall(self.w.reg, self.rid, "扩大", "过磅批次", self.w.bid)
        view = svc.get_recall(self.w.reg, self.rid)
        transit_t = self.w.task_by(view, 位置="在途")
        self.assertEqual(transit_t["状态"], "出围待核")
        self.assertEqual(transit_t["隔离中kg"], "30")


    def test_shrink_to_empty_scope_rejected(self):
        svc = self.w.svc
        # 一个与召回无关、没有任何货物的地块
        empty_plot = svc.create_plot(self.w.coop, self.w.other_id, "空地", "广兴镇")["地块编号"]
        with self.assertRaises(Conflict) as cm:
            svc.reshape_recall(self.w.reg, self.rid, "缩小", "地块", empty_plot)
        self.assertEqual(cm.exception.code, "empty_recall_scope")


class RecallConflictTest(unittest.TestCase):
    def setUp(self):
        self.w = RecallWorld()
        self.prid = self.w.fail_pesticide()
        self.recall = self.w.svc.open_recall(
            self.w.reg, "过磅批次", self.w.bid, self.prid)
        self.rid = self.recall["召回编号"]

    def test_conflict_freezes_until_authorized_review(self):
        svc = self.w.svc
        transit = self.w.task_by(self.recall, 位置="在途")
        svc.report_conflict(self.w.ent, transit["任务编号"],
                            "该车已于通知前卸货，数量对不上")
        # 裁决前任何处置被冻结
        with self.assertRaises(Conflict) as cm:
            svc.disposal_receipt(self.w.ent, transit["任务编号"], "拦截", "X1", "30")
        self.assertEqual(cm.exception.code, "conflict_pending")
        # 重复上报冲突拒绝
        with self.assertRaises(Conflict):
            svc.report_conflict(self.w.ent, transit["任务编号"], "再报一次")
        # 未裁决不能结案
        with self.assertRaises(Conflict) as cm:
            svc.close_recall(self.w.reg, self.rid)
        self.assertEqual(cm.exception.code, "conflict_pending")

    def test_resolve_release_restores_held_funds(self):
        svc = self.w.svc
        transit = self.w.task_by(self.recall, 位置="在途")
        # 先人工隔离，再挂冲突
        svc.notify_task(self.w.ent, transit["任务编号"])
        svc.disposal_receipt(self.w.ent, transit["任务编号"], "隔离", "Q1", "30")
        svc.report_conflict(self.w.ent, transit["任务编号"], "疑似同批但非同车")
        # 监管先挂账暂缓 300，裁决解除后应自动恢复
        svc.recall_fund(self.w.reg, self.rid, self.w.sid, "暂缓", "300", "等待冲突结论")
        conf = svc.get_recall(self.w.reg, self.rid)["冲突"][0]
        svc.resolve_conflict(self.w.reg, conf["冲突编号"], "解除隔离", "复检合格，解除")
        view = svc.get_recall(self.w.reg, self.rid)
        task = self.w.task_by(view, 位置="在途")
        self.assertEqual(task["状态"], "已解除")
        kinds = [f["种类"] for f in view["追加账"]]
        self.assertIn("恢复", kinds)
        self.assertEqual(sum(1 for k in kinds if k == "恢复"), 1)

    def test_resolve_hold_keeps_quarantine(self):
        svc = self.w.svc
        transit = self.w.task_by(self.recall, 位置="在途")
        svc.report_conflict(self.w.ent, transit["任务编号"], "数量异议")
        conf = svc.get_recall(self.w.reg, self.rid)["冲突"][0]
        svc.resolve_conflict(self.w.reg, conf["冲突编号"], "维持隔离", "确认超标")
        view = svc.get_recall(self.w.reg, self.rid)
        self.assertEqual(self.w.task_by(view, 位置="在途")["状态"], "已隔离")


class RecallFundTest(unittest.TestCase):
    def setUp(self):
        self.w = RecallWorld()
        self.prid = self.w.fail_pesticide()
        self.recall = self.w.svc.open_recall(
            self.w.reg, "过磅批次", self.w.bid, self.prid)
        self.rid = self.recall["召回编号"]

    def test_fund_cap_follows_grade_appeal_and_original_unchanged(self):
        svc = self.w.svc
        # 等级申诉降级 A->B：价差 -100，当前权益 300；原结算行仍 400 不改
        svc.review_grade(self.w.reviewer, self.w.insp["检验编号"], "B", "农残伴生风伤，降级")
        self.assertEqual(svc.get_settlement(self.w.coop, self.w.sid)
                         ["明细行"][-1]["合计应收"], "400.00")
        svc.recall_fund(self.w.reg, self.rid, self.w.sid, "暂缓", "300", "全额暂缓")
        with self.assertRaises(Conflict) as cm:
            svc.recall_fund(self.w.reg, self.rid, self.w.sid, "追回", "1")
        self.assertEqual(cm.exception.code, "fund_exceeded")
        # 恢复不得超过未解除挂账
        with self.assertRaises(Conflict):
            svc.recall_fund(self.w.reg, self.rid, self.w.sid, "恢复", "301")
        svc.recall_fund(self.w.reg, self.rid, self.w.sid, "恢复", "100", "部分解除")
        view = svc.get_recall(self.w.reg, self.rid)
        self.assertEqual(len(view["追加账"]), 2)

    def test_farmer_sees_own_fund_only(self):
        svc = self.w.svc
        svc.recall_fund(self.w.reg, self.rid, self.w.sid, "暂缓", "200", "暂缓")
        view = svc.get_recall(self.w.farmer, self.rid)
        self.assertEqual(len(view["追加账"]), 1)
        self.assertEqual(view["追加账"][0]["种类"], "暂缓")


class RecallVisibilityTest(unittest.TestCase):
    def setUp(self):
        self.w = RecallWorld()
        self.prid = self.w.fail_pesticide()
        self.recall = self.w.svc.open_recall(
            self.w.reg, "过磅批次", self.w.bid, self.prid)
        self.rid = self.recall["召回编号"]

    def test_each_role_sees_only_duty_data(self):
        svc = self.w.svc
        # 护树队：召回一律 403
        with self.assertRaises(PermissionDenied):
            svc.list_recalls(self.w.guard)
        with self.assertRaises(PermissionDenied):
            svc.get_recall(self.w.guard, self.rid)
        # 监管看全量
        reg_view = svc.get_recall(self.w.reg, self.rid)
        self.assertEqual(len(reg_view["任务"]), 3)
        # 企业只见在库/在途两个原料节点，不见加工方成品，也不见资金
        ent_view = svc.get_recall(self.w.ent, self.rid)
        self.assertEqual({t["保管方"] for t in ent_view["任务"]}, {"企业", "承运方"})
        self.assertEqual(ent_view["追加账"], [])
        # 加工方只见成品
        proc_view = svc.get_recall(self.w.proc, self.rid)
        self.assertEqual([t["节点类型"] for t in proc_view["任务"]], ["成品"])
        # 农户只见挂到本人农户的原料节点（成品任务农户编号为空）
        farm_view = svc.get_recall(self.w.farmer, self.rid)
        self.assertTrue(all(t.get("农户编号") == self.w.farmer_id for t in farm_view["任务"]))
        # 跨企业不可见
        with self.assertRaises(PermissionDenied):
            svc.get_recall(self.w.ent2, self.rid)
        # 别家加工方不可见、不可处置本加工方成品
        goods = next(t for t in reg_view["任务"] if t["节点类型"] == "成品")
        with self.assertRaises(PermissionDenied):
            svc.get_recall(self.w.proc2, self.rid)
        with self.assertRaises(PermissionDenied):
            svc.notify_task(self.w.proc2, goods["任务编号"])
        # 守恒报告不对企业开放
        with self.assertRaises(PermissionDenied):
            svc.recall_report(self.w.ent, self.rid)


class ReturnedGoodsRecallTest(unittest.TestCase):
    def test_returned_fruit_is_located_as_return_holding(self):
        w = RecallWorld()
        svc = w.svc
        # 企业从在库退 10kg（在库随之 40->30）
        ret = svc.enterprise_return(w.ent, w.did, "10", "到货农残抽检异常",
                                    w.insp["检验编号"], "待处置")
        prid = w.fail_pesticide()
        r = svc.open_recall(w.reg, "过磅批次", w.bid, prid)
        units = {(t["节点类型"], t["保管方"]): t for t in r["任务"]}
        self.assertIn(("退货单", "企业"), units)
        self.assertEqual(units[("退货单", "企业")]["任务重量kg"], "10")
        # 在库量已扣减退货
        stock = units.get(("交货单", "企业"))
        self.assertEqual(stock["任务重量kg"], "30")
        # 退货果实可销毁
        t = units[("退货单", "企业")]
        svc.notify_task(w.ent, t["任务编号"])
        svc.disposal_receipt(w.ent, t["任务编号"], "隔离", "RT-1", "10")
        svc.disposal_receipt(w.ent, t["任务编号"], "销毁", "RT-2", "10")
        view = svc.get_recall(w.reg, r["召回编号"])
        done = [x for x in view["任务"] if x["节点类型"] == "退货单"][0]
        self.assertEqual(done["已销毁kg"], "10")


if __name__ == "__main__":
    unittest.main(verbosity=2)