"""HTTP 层契约测试：鉴权、错误码映射、断网重传与护树队隔离走真实端口。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from domain import HeritageCitrusService
from service import make_handler


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = HeritageCitrusService()
        handler = make_handler(cls.service)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def call(self, method: str, path: str, payload=None, token=None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["X-Actor-Token"] = token
        req = Request(f"{self.base}{quote(path, safe='/?=&')}", data=data,
                      headers=headers, method=method)
        try:
            with urlopen(req, timeout=3) as resp:
                return resp.status, json.load(resp)
        except HTTPError as exc:
            return exc.code, json.load(exc)

    def setUp(self):
        # 每个用例重新登记角色（编号会累计，但语义互不影响）
        _, = (self.call("POST", "/actors",
                        {"token": "coop", "姓名": "秦会计", "角色": "合作社"}),)
        self.call("POST", "/actors", {"token": "reviewer", "姓名": "严复核", "角色": "质量复核人"})
        self.call("POST", "/actors", {"token": "guard", "姓名": "护树员老吴", "角色": "护树队"})
        self.call("POST", "/actors", {"token": "ent", "姓名": "广兴饮料厂", "角色": "收购企业"})
        _, farmer = self.call("POST", "/farmers", {"姓名": "梁果农"}, token="coop")
        self.farmer_id = farmer["农户编号"]
        self.call("POST", "/actors",
                  {"token": "farmer", "姓名": "梁果农", "角色": "果农",
                   "农户编号": self.farmer_id})
        _, plot = self.call("POST", "/plots",
                            {"农户编号": self.farmer_id, "名称": "梁家湾坡地",
                             "地点": "广兴镇"}, token="coop")
        self.plot_id = plot["地块编号"]
        _, tree = self.call("POST", "/trees",
                            {"地块编号": self.plot_id, "名称": "连片老红橘",
                             "树种": "红橘", "树龄年": 60, "保护级别": "普通老树"},
                            token="coop")
        self.tree_id = tree["编号"]
        _, heritage = self.call("POST", "/trees",
                                {"地块编号": self.plot_id, "名称": "百年母树群",
                                 "树种": "红橘", "树龄年": 130,
                                 "保护级别": "百年保护树"}, token="coop")
        self.heritage_id = heritage["编号"]
        self.call("POST", "/price-rules",
                  {"生效时间": "2026-08-01T00:00:00+00:00",
                   "保护价": {"A": "4.00", "B": "3.00"}}, token="coop")
        _, contract = self.call("POST", "/contracts",
                                {"农户编号": self.farmer_id, "企业编号": "广兴饮料厂",
                                 "签约时间": "2026-08-05T00:00:00+00:00",
                                 "约定等级": ["A", "B"], "季": "2026秋"}, token="coop")
        self.contract_id = contract["合约编号"]
        self.call("POST", "/quotas",
                  {"树群编号": self.heritage_id, "季": "2026秋",
                   "配额重量kg": "100", "配额次数": 2}, token="coop")

    def test_health_is_open(self):
        status, body = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_endpoints_require_token(self):
        status, body = self.call("GET", "/batches")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")
        status, _ = self.call("POST", "/weigh", {"批次编号": "x"})
        self.assertEqual(status, 401)

    def test_end_to_end_flow_with_offline_resend_and_settlement(self):
        _, batch = self.call("POST", "/batches",
                             {"地块编号": self.plot_id, "采收日期": "2026-09-01",
                              "季": "2026秋"}, token="coop")
        bid = batch["批次编号"]

        payload = {"批次编号": bid, "树群编号": self.tree_id, "过磅流水号": "NET-1",
                   "毛重kg": "210", "皮重kg": "10", "断网离线": True, "设备号": "地磅-02"}
        status, ticket = self.call("POST", "/weigh", payload, token="coop")
        self.assertEqual(status, 201)
        self.assertEqual(ticket["重量kg"], "200")
        # 断网恢复后重放：幂等
        _, again = self.call("POST", "/weigh", payload, token="coop")
        self.assertTrue(again["幂等命中"])
        self.assertEqual(again["磅单编号"], ticket["磅单编号"])

        _, insp = self.call("POST", "/inspections",
                            {"磅单编号": ticket["磅单编号"], "等级": "A",
                             "判定依据": "糖度外观达标"}, token="reviewer")
        _, delivery = self.call("POST", "/deliveries",
                                {"批次编号": bid, "企业编号": "广兴饮料厂",
                                 "合约编号": self.contract_id,
                                 "交货时间": "2026-09-02T08:00:00+00:00"}, token="coop")
        _, settlement = self.call("POST", "/settlements",
                                  {"交货单编号": delivery["交货单编号"]}, token="coop")
        self.assertEqual(settlement["明细行"][-1]["合计应收"], "800.00")

        # 重复结算 → 409
        status, body = self.call("POST", "/settlements",
                                 {"交货单编号": delivery["交货单编号"]}, token="coop")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "already_settled")

    def test_guard_permission_boundary_over_http(self):
        # 护树队上报病害：允许
        status, _ = self.call("POST", "/reports",
                              {"树群编号": self.heritage_id, "类型": "病害",
                               "描述": "发现病斑"}, token="guard")
        self.assertEqual(status, 201)
        # 护树队查看结算：403
        status, body = self.call("GET", "/ledgers/农户结算", token="guard")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "forbidden")
        # 护树队过磅：403
        _, batch = self.call("POST", "/batches",
                             {"地块编号": self.plot_id, "采收日期": "2026-09-01",
                              "季": "2026秋"}, token="coop")
        status, _ = self.call("POST", "/weigh",
                              {"批次编号": batch["批次编号"], "树群编号": self.tree_id,
                               "过磅流水号": "Z1", "重量kg": "1"}, token="guard")
        self.assertEqual(status, 403)
        # 护树队的树群视图被裁剪
        _, view = self.call("GET", f"/trees/{self.heritage_id}", token="guard")
        self.assertNotIn("本季已采重量kg", view)

    def test_heritage_quota_enforced_over_http(self):
        _, batch = self.call("POST", "/batches",
                             {"地块编号": self.plot_id, "采收日期": "2026-09-01",
                              "季": "2026秋"}, token="coop")
        bid = batch["批次编号"]
        status, _ = self.call("POST", "/reservations",
                              {"批次编号": bid, "树群编号": self.heritage_id,
                               "重量kg": "101"}, token="coop")
        self.assertEqual(status, 409)
        self.call("POST", "/reservations",
                  {"批次编号": bid, "树群编号": self.heritage_id, "重量kg": "50"},
                  token="coop")
        self.call("POST", "/weigh",
                  {"批次编号": bid, "树群编号": self.heritage_id,
                   "过磅流水号": "H1", "重量kg": "50"}, token="coop")
        status, body = self.call("POST", "/weigh",
                                 {"批次编号": bid, "树群编号": self.heritage_id,
                                  "过磅流水号": "H2", "重量kg": "1"}, token="coop")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "over_reservation")

    def test_trace_and_validation_errors(self):
        _, batch = self.call("POST", "/batches",
                             {"地块编号": self.plot_id, "采收日期": "2026-09-01",
                              "季": "2026秋"}, token="coop")
        bid = batch["批次编号"]
        _, ticket = self.call("POST", "/weigh",
                              {"批次编号": bid, "树群编号": self.tree_id,
                               "过磅流水号": "T1", "重量kg": "100"}, token="coop")
        self.call("POST", "/inspections",
                  {"磅单编号": ticket["磅单编号"], "等级": "A",
                   "判定依据": "达标"}, token="reviewer")
        _, delivery = self.call("POST", "/deliveries",
                                {"批次编号": bid, "企业编号": "广兴饮料厂",
                                 "合约编号": self.contract_id,
                                 "交货时间": "2026-09-02T08:00:00+00:00"}, token="coop")
        status, chain = self.call("GET", f"/trace?delivery={delivery['交货单编号']}",
                                  token="ent")
        self.assertEqual(status, 200)
        self.assertEqual(chain["地块"]["地块编号"], self.plot_id)
        self.assertEqual(chain["受益农户"]["农户编号"], self.farmer_id)

        # 缺字段 → 422
        status, body = self.call("POST", "/plots", {"名称": "无名地块"}, token="coop")
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "validation_failed")
        # 未知路由 → 404
        status, _ = self.call("GET", "/nope", token="coop")
        self.assertEqual(status, 404)


class RecallApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = HeritageCitrusService()
        handler = make_handler(cls.service)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def call(self, method: str, path: str, payload=None, token=None):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["X-Actor-Token"] = token
        req = Request(f"{self.base}{quote(path, safe='/?=&')}", data=data,
                      headers=headers, method=method)
        try:
            with urlopen(req, timeout=3) as resp:
                return resp.status, json.load(resp)
        except HTTPError as exc:
            return exc.code, json.load(exc)

    def setUp(self):
        self.call("POST", "/actors", {"token": "coop", "姓名": "秦会计", "角色": "合作社"})
        self.call("POST", "/actors", {"token": "reviewer", "姓名": "严复核", "角色": "质量复核人"})
        self.call("POST", "/actors", {"token": "guard", "姓名": "护树员老吴", "角色": "护树队"})
        self.call("POST", "/actors", {"token": "ent", "姓名": "广兴饮料厂", "角色": "收购企业"})
        self.call("POST", "/actors", {"token": "proc", "姓名": "陈皮加工厂", "角色": "加工方"})
        self.call("POST", "/actors", {"token": "reg", "姓名": "监管员郑直", "角色": "监管人员"})
        _, farmer = self.call("POST", "/farmers", {"姓名": "梁果农"}, token="coop")
        self.farmer_id = farmer["农户编号"]
        self.call("POST", "/actors",
                  {"token": "farmer", "姓名": "梁果农", "角色": "果农",
                   "农户编号": self.farmer_id})
        _, plot = self.call("POST", "/plots",
                            {"农户编号": self.farmer_id, "名称": "坡地", "地点": "广兴镇"},
                            token="coop")
        self.plot_id = plot["地块编号"]
        _, tree = self.call("POST", "/trees",
                            {"地块编号": self.plot_id, "名称": "老树", "树种": "红橘",
                             "树龄年": 60, "保护级别": "普通老树"}, token="coop")
        self.tree_id = tree["编号"]
        self.call("POST", "/price-rules",
                  {"生效时间": "2026-08-01T00:00:00+00:00",
                   "保护价": {"A": "4.00", "B": "3.00"}}, token="coop")
        _, contract = self.call("POST", "/contracts",
                                {"农户编号": self.farmer_id, "企业编号": "广兴饮料厂",
                                 "签约时间": "2026-08-05T00:00:00+00:00",
                                 "约定等级": ["A", "B"], "季": "2026秋"}, token="coop")
        self.contract_id = contract["合约编号"]

    def _build_chain(self):
        """交货100 → 60发加工(30在途/30到厂投) → 成品20；在库余40。"""
        _, batch = self.call("POST", "/batches",
                             {"地块编号": self.plot_id, "采收日期": "2026-09-01",
                              "季": "2026秋"}, token="coop")
        bid = batch["批次编号"]
        _, ticket = self.call("POST", "/weigh",
                              {"批次编号": bid, "树群编号": self.tree_id,
                               "过磅流水号": "W1", "重量kg": "100"}, token="coop")
        _, insp = self.call("POST", "/inspections",
                            {"磅单编号": ticket["磅单编号"], "等级": "A",
                             "判定依据": "达标"}, token="reviewer")
        _, delivery = self.call("POST", "/deliveries",
                                {"批次编号": bid, "企业编号": "广兴饮料厂",
                                 "合约编号": self.contract_id,
                                 "交货时间": "2026-09-02T08:00:00+00:00"}, token="coop")
        did = delivery["交货单编号"]
        _, settlement = self.call("POST", "/settlements",
                                  {"交货单编号": did}, token="coop")
        self.call("POST", "/processing-ship",
                  {"交货单编号": did, "重量kg": "60"}, token="ent")
        self.call("POST", "/processing-arrive",
                  {"交货单编号": did, "重量kg": "30"}, token="proc")
        _, pb = self.call("POST", "/processing-batches",
                          {"制品": "陈皮",
                           "原料行": [{"交货单编号": did, "投入重量kg": "30"}],
                           "成品": [{"制品": "陈皮", "重量kg": "20"}]}, token="proc")
        gid = pb["成品"][0]["成品编号"]
        return bid, insp["检验编号"], did, settlement["结算编号"], gid

    def test_recall_end_to_end_over_http(self):
        bid, iid, did, sid, gid = self._build_chain()
        # 农残复核不合格
        status, pr = self.call("POST", "/pesticide-reviews",
                               {"检验编号": iid, "项目": "克百威", "结果": "不合格",
                                "实测值": "0.05", "限量值": "0.02",
                                "判定依据": "复测超标"}, token="reg")
        self.assertEqual(status, 201)
        prid = pr["农残编号"]
        # 合格结论不能召回：另发一条合格会把旧记录置非现行，故直接用权限/403验证
        # 从过磅批次发起
        status, recall = self.call("POST", "/recalls",
                                   {"起点类型": "过磅批次", "起点编号": bid,
                                    "农残编号": prid}, token="reg")
        self.assertEqual(status, 201)
        rid = recall["召回编号"]
        self.assertEqual(len(recall["任务"]), 3)
        transit = next(t for t in recall["任务"] if t["位置"] == "在途")

        # 通知幂等
        s1, n1 = self.call("POST", "/recall-notify",
                           {"任务编号": transit["任务编号"]}, token="ent")
        s2, n2 = self.call("POST", "/recall-notify",
                           {"任务编号": transit["任务编号"]}, token="ent")
        self.assertEqual((s1, s2), (201, 201))
        self.assertFalse(n1["幂等命中"])
        self.assertTrue(n2["幂等命中"])

        # 离线拦截回执，重复流水号幂等
        receipt = {"任务编号": transit["任务编号"], "动作": "拦截",
                   "回执流水号": "RC-1", "重量kg": "30", "离线补录": True}
        self.call("POST", "/recall-receipts", receipt, token="ent")
        _, again = self.call("POST", "/recall-receipts", receipt, token="ent")
        self.assertTrue(again["幂等命中"])
        self.call("POST", "/recall-receipts",
                  {"任务编号": transit["任务编号"], "动作": "销毁",
                   "回执流水号": "RC-2", "重量kg": "30"}, token="ent")
        # 在库40 系统隔离 -> 企业销毁
        stock = next(t for t in self.call("GET", f"/recalls/{rid}", token="reg")[1]["任务"]
                     if t["位置"] == "在库" and t["保管方"] == "企业")
        self.call("POST", "/recall-receipts",
                  {"任务编号": stock["任务编号"], "动作": "销毁",
                   "回执流水号": "RC-3", "重量kg": "40"}, token="ent")
        # 成品20 加工方销毁
        goods = next(t for t in self.call("GET", f"/recalls/{rid}", token="reg")[1]["任务"]
                     if t["节点类型"] == "成品")
        self.call("POST", "/recall-receipts",
                  {"任务编号": goods["任务编号"], "动作": "销毁",
                   "回执流水号": "RC-4", "重量kg": "20"}, token="proc")

        # 追加账
        self.call("POST", "/recall-funds",
                  {"召回编号": rid, "结算编号": sid, "种类": "暂缓",
                   "金额": "200", "摘要": "暂缓付款"}, token="reg")

        # 守恒报告并结案
        status, report = self.call("GET", f"/recalls/{rid}/report", token="reg")
        self.assertEqual(status, 200)
        self.assertTrue(report["结案报告"]["数量守恒"])
        self.assertEqual(report["结案报告"]["去向"]["销毁kg"], "90")
        status, closed = self.call("POST", "/recall-close", {"召回编号": rid}, token="reg")
        self.assertEqual(status, 201)
        self.assertEqual(closed["状态"], "已结案")

    def test_recall_role_boundaries_over_http(self):
        bid, iid, did, sid, gid = self._build_chain()
        _, pr = self.call("POST", "/pesticide-reviews",
                          {"检验编号": iid, "项目": "克百威", "结果": "不合格",
                           "实测值": "0.05", "限量值": "0.02", "判定依据": "x"},
                          token="reg")
        # 企业无权发起召回 → 403
        status, _ = self.call("POST", "/recalls",
                              {"起点类型": "过磅批次", "起点编号": bid,
                               "农残编号": pr["农残编号"]}, token="ent")
        self.assertEqual(status, 403)
        # 护树队连召回列表都不可见 → 403
        status, body = self.call("GET", "/recalls", token="guard")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "forbidden")
        # 监管正常发起
        _, recall = self.call("POST", "/recalls",
                              {"起点类型": "成品", "起点编号": gid,
                               "农残编号": pr["农残编号"]}, token="reg")
        rid = recall["召回编号"]
        # 护树队不可见守恒报告
        status, _ = self.call("GET", f"/recalls/{rid}/report", token="guard")
        self.assertEqual(status, 403)
        # 企业也不可见跨方守恒报告
        status, _ = self.call("GET", f"/recalls/{rid}/report", token="ent")
        self.assertEqual(status, 403)
        # 加工方是成品保管方，可见该召回任务
        status, view = self.call("GET", f"/recalls/{rid}", token="proc")
        self.assertEqual(status, 200)
        self.assertEqual([t["节点类型"] for t in view["任务"]], ["成品"])

    def test_conflict_blocks_disposal_until_resolved(self):
        bid, iid, did, sid, gid = self._build_chain()
        _, pr = self.call("POST", "/pesticide-reviews",
                          {"检验编号": iid, "项目": "克百威", "结果": "不合格",
                           "实测值": "0.05", "限量值": "0.02", "判定依据": "x"},
                          token="reg")
        _, recall = self.call("POST", "/recalls",
                              {"起点类型": "过磅批次", "起点编号": bid,
                               "农残编号": pr["农残编号"]}, token="reg")
        rid = recall["召回编号"]
        transit = next(t for t in recall["任务"] if t["位置"] == "在途")
        self.call("POST", "/recall-conflicts",
                  {"任务编号": transit["任务编号"], "上报内容": "数量对不上"}, token="ent")
        # 裁决前处置 → 409 conflict_pending
        status, body = self.call("POST", "/recall-receipts",
                                 {"任务编号": transit["任务编号"], "动作": "拦截",
                                  "回执流水号": "X1", "重量kg": "30"}, token="ent")
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "conflict_pending")
        # 监管查看冲突并裁决维持隔离
        _, full = self.call("GET", f"/recalls/{rid}", token="reg")
        cid = full["冲突"][0]["冲突编号"]
        status, _ = self.call("POST", "/conflict-resolve",
                              {"冲突编号": cid, "裁决": "维持隔离",
                               "裁决意见": "确认超标"}, token="reg")
        self.assertEqual(status, 201)


if __name__ == "__main__":
    unittest.main(verbosity=2)
