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
        self.call("POST", "/actors", {"token": "proc", "姓名": "陈皮加工厂", "角色": "加工方"})
        self.call("POST", "/actors", {"token": "reg", "姓名": "监管老冯", "角色": "监管人员"})
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


    def _full_delivery(self, slip="R1", weight="100"):
        """过磅→检验→交货（不结算），返回各编号。"""
        _, batch = self.call("POST", "/batches",
                             {"地块编号": self.plot_id, "采收日期": "2026-09-01",
                              "季": "2026秋"}, token="coop")
        _, ticket = self.call("POST", "/weigh",
                              {"批次编号": batch["批次编号"], "树群编号": self.tree_id,
                               "过磅流水号": slip, "重量kg": weight}, token="coop")
        _, insp = self.call("POST", "/inspections",
                            {"磅单编号": ticket["磅单编号"], "等级": "A",
                             "判定依据": "达标"}, token="reviewer")
        _, delivery = self.call("POST", "/deliveries",
                                {"批次编号": batch["批次编号"], "企业编号": "广兴饮料厂",
                                 "合约编号": self.contract_id,
                                 "交货时间": "2026-09-02T08:00:00+00:00"}, token="coop")
        return batch, ticket, insp, delivery

    def test_recall_lifecycle_over_http(self):
        _, ticket, insp, delivery = self._full_delivery()
        _, settlement = self.call("POST", "/settlements",
                                  {"交货单编号": delivery["交货单编号"]}, token="coop")

        # 加工方建档加工批次与成品
        _, pb = self.call("POST", "/processing-batches",
                          {"制品": "陈皮",
                           "投入明细": [{"来源类型": "交货",
                                        "来源单号": delivery["交货单编号"],
                                        "重量kg": "100"}]}, token="proc")
        self.assertEqual(pb["投入合计kg"], "100")
        _, good = self.call("POST", "/goods",
                            {"加工批次编号": pb["加工批次编号"], "名称": "陈皮罐",
                             "成品批次号": "CP-1", "重量kg": "60"}, token="proc")

        # 从成品发起召回（只有监管人员可以）
        status, body = self.call("POST", "/recalls", {
            "节点类型": "成品", "节点编号": good["成品编号"],
            "原因": "成品检出农残", "检验依据编号": insp["检验编号"]}, token="reg")
        self.assertEqual(status, 201)
        rid = body["召回编号"]
        task = body["任务"][0]
        self.assertEqual(task["受控数量kg"], "60")
        # 护树队发起召回 → 403
        status, _ = self.call("POST", "/recalls", {
            "节点类型": "成品", "节点编号": good["成品编号"],
            "原因": "x", "检验依据编号": insp["检验编号"]}, token="guard")
        self.assertEqual(status, 403)

        # 通知 → 离线回执 → 重放只一次效果
        self.call("POST", f"/recalls/{rid}/notify",
                  {"任务编号": task["任务编号"], "渠道": "短信", "内容": "销毁"},
                  token="reg")
        status, rep = self.call("POST", f"/recalls/{rid}/receipts",
                                {"任务编号": task["任务编号"], "回执号": "P-1",
                                 "动作": "销毁", "数量kg": "60",
                                 "断网离线": True}, token="proc")
        self.assertEqual(status, 201)
        self.assertFalse(rep["重复"])
        _, replay = self.call("POST", f"/recalls/{rid}/receipts",
                              {"任务编号": task["任务编号"], "回执号": "P-1",
                               "动作": "销毁", "数量kg": "60"}, token="proc")
        self.assertTrue(replay["重复"])

        # 资金暂缓沿父子关系摊回磅单：60kg × 4 元
        _, hold = self.call("POST", f"/recalls/{rid}/funds/hold",
                            {"结算编号": settlement["结算编号"]}, token="reg")
        self.assertEqual(hold["合计金额"], "240.00")
        # 历史结算不改写
        _, settled = self.call("GET", f"/settlements/{settlement['结算编号']}",
                               token="coop")
        self.assertEqual(settled["明细行"][-1]["合计应收"], "400.00")

        # 结案：守恒、资金变化
        status, report = self.call("POST", f"/recalls/{rid}/close", {}, token="reg")
        self.assertEqual(status, 200)
        self.assertEqual(report["数量守恒"]["销毁kg"], "60")
        self.assertEqual(report["数量守恒"]["差额kg"], "0")
        self.assertEqual(report["资金变化"]["暂缓"], "240.00")

    def test_recall_conflict_authorization_over_http(self):
        _, ticket, insp, delivery = self._full_delivery()
        _, recall = self.call("POST", "/recalls", {
            "节点类型": "过磅批次", "节点编号": ticket["磅单编号"],
            "原因": "农残", "检验依据编号": insp["检验编号"]}, token="reg")
        rid = recall["召回编号"]
        task = recall["任务"][0]
        self.call("POST", f"/recalls/{rid}/notify",
                  {"任务编号": task["任务编号"], "渠道": "短信", "内容": "隔离"},
                  token="reg")
        # 企业上报 120kg，系统受控 100kg → 冲突挂起
        _, rep = self.call("POST", f"/recalls/{rid}/receipts",
                           {"任务编号": task["任务编号"], "回执号": "X1",
                            "动作": "销毁", "数量kg": "120"}, token="ent")
        cid = rep["冲突"]["冲突编号"]
        # 企业授权 → 403
        status, _ = self.call("POST", f"/recalls/{rid}/conflicts/{cid}",
                              {"决定": "调整", "认定数量kg": "120"}, token="ent")
        self.assertEqual(status, 403)
        # 监管维持后按 100kg 重报，冲突解除
        self.call("POST", f"/recalls/{rid}/conflicts/{cid}",
                  {"决定": "维持", "授权意见": "以受控量为准"}, token="reg")
        status, rep2 = self.call("POST", f"/recalls/{rid}/receipts",
                                 {"任务编号": task["任务编号"], "回执号": "X2",
                                  "动作": "销毁", "数量kg": "100"}, token="ent")
        self.assertEqual(status, 201)
        self.assertIsNone(rep2["冲突"])

    def test_recall_role_views_are_scoped_over_http(self):
        _, ticket, insp, delivery = self._full_delivery()
        _, recall = self.call("POST", "/recalls", {
            "节点类型": "过磅批次", "节点编号": ticket["磅单编号"],
            "原因": "农残", "检验依据编号": insp["检验编号"]}, token="reg")
        rid = recall["召回编号"]
        # 企业看自己的召回任务（同一测试服务上其他用例也可能给企业派单）
        status, mine = self.call("GET", "/recall-tasks", token="ent")
        self.assertEqual(status, 200)
        self.assertIn(recall["任务"][0]["任务编号"],
                      {t["任务编号"] for t in mine["记录"]})
        # 加工方与本次召回无关：不持有该召回的任务
        _, proc_tasks = self.call("GET", "/recall-tasks", token="proc")
        self.assertFalse(
            any(t["召回编号"] == rid for t in proc_tasks["记录"]))
        # 护树队 403
        self.assertEqual(self.call("GET", "/recalls", token="guard")[0], 403)
        # 监管列表可见
        _, recalls = self.call("GET", "/recalls", token="reg")
        self.assertTrue(any(r["召回编号"] == rid for r in recalls["记录"]))
        # 单任务详情：企业可取
        status, _ = self.call(
            "GET", f"/recall-tasks/{recall['任务'][0]['任务编号']}", token="ent")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
