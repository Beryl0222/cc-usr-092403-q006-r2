"""老红橘保护性收购 —— 领域层。

设计要点
========

* **连续溯源链**：地块 → 树群 → 管护记录 → 采收批次 → 磅单 → 检验 → 企业交货，
  每一环节都引用上一环节的编号，任何一环缺失都无法进入下一环节。
* **每公斤只结算一次**：磅单以 ``(批次, 过磅流水号)`` 幂等，断网重传同一批果不会
  重复入库；结算以磅单为粒度，已结算磅单被结算流水永久引用。
* **保护树不得越界**：保护树群有按采收季生效的配额（重量 + 次数），配额按批次
  预占，磅单回冲；预占超额或磅单超出所属批次的预占额度都会被拒绝。
* **价格不回溯**：农户应收按 *签约时规则 + 交货时适用规则* 计算并写入结算流水，
  之后市场价变动只生成新价规版本，不改写历史流水。
* **复核留痕**：质量复核可改级，但原样本、原等级、原判定依据保留，等级变化产生
  独立的价差调整（补付或扣回），不覆盖原结算。
* **四条独立流水**：农户结算 / 企业退货 / 运输损耗 / 果肉加工路线（陈皮等）分账记录，
  互不混记。
* **农残复核异常与召回**：召回可从地块/过磅批次/加工批次/成品任一节点发起，沿
  「来源 → 投入/成品」父子数量关系形成带版本号的召回范围，定位每个货权节点的当前
  保管方并分派隔离/拦截/退回/销毁/解除/替代交付任务。扩范围补建任务、缩范围不自动
  解除人工隔离；离线回执与重复通知按回执号只产生一次业务效果；处置数量冲突挂起等待
  监管授权复核；原检验与历史结算永不变写，资金一律以追加账记录暂缓/追回/恢复；结案
  必须交代数量守恒、未回执节点与每笔资金变化。
* **角色隔离**：护树队只能上报病害与违规采摘，读取树群管护视图，无法接触农户
  结算明细；不同农户之间也互不可见。
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

# ---------------------------------------------------------------------------
# 角色
# ---------------------------------------------------------------------------

ROLE_FARMER = "果农"          # 查看本人地块/批次/应收与本人召回任务
ROLE_COOP = "合作社"          # 全链管理与结算
ROLE_REVIEWER = "质量复核人"  # 取样、复核改级
ROLE_GUARD = "护树队"         # 仅上报病害、违规采摘
ROLE_ENTERPRISE = "收购企业"  # 交货对接、成品原料反查
ROLE_PROCESSOR = "加工方"     # 加工批次、成品与召回处置
ROLE_REGULATOR = "监管人员"   # 发起召回、授权复核、结案

# 结算明细属于合作社财务域，护树队无权查看
SETTLER_ROLES = {ROLE_COOP}
SETTLE_DETAIL_VIEWERS = {ROLE_COOP}

# 护树队可见的最小树群视图
GUARD_TREE_VIEW = {"编号", "树群编号", "地块编号", "保护级别", "树种", "树龄年", "健康状态"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _money(value) -> str:
    """金额统一为两位小数字符串，避免浮点误差。"""
    return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class DomainError(Exception):
    """所有可预期的业务拒绝，HTTP 层映射为 4xx。"""

    status = 400

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class NotFound(DomainError):
    status = 404

    def __init__(self, what: str):
        super().__init__("not_found", f"{what}不存在或无权访问")


class Conflict(DomainError):
    status = 409

    def __init__(self, code: str, message: str):
        super().__init__(code, message)


class AuthError(DomainError):
    status = 401

    def __init__(self):
        super().__init__("unauthorized", "缺少有效身份令牌")


class PermissionDenied(DomainError):
    status = 403

    def __init__(self, what: str = "该操作"):
        super().__init__("forbidden", f"无权{what}")


class ValidationFailed(DomainError):
    status = 422

    def __init__(self, message: str):
        super().__init__("validation_failed", message)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class Actor:
    token: str
    name: str
    role: str
    farmer_id: str | None = None  # 果农身份绑定的农户编号


@dataclass
class Plot:
    编号: str
    农户编号: str
    名称: str
    地点: str


@dataclass
class TreeGroup:
    编号: str
    地块编号: str
    名称: str
    树种: str
    树龄年: int
    保护级别: str          # 普通老树 / 百年保护树
    健康状态: str = "正常"
    本季已采重量: Decimal = Decimal("0")
    本季已采次数: int = 0


@dataclass
class CareLog:
    编号: str
    树群编号: str
    日期: str
    事项: str
    记录人: str
    病害: bool = False
    病害描述: str = ""


@dataclass
class HarvestBatch:
    编号: str
    地块编号: str
    农户编号: str
    采收日期: str
    季: str
    状态: str = "采收中"           # 采收中/待检验/待收购/已交货
    预占重量: Decimal = Decimal("0")
    交货单编号: str | None = None
    来源树群: list[str] = field(default_factory=list)


@dataclass
class WeighTicket:
    编号: str
    批次编号: str
    树群编号: str
    过磅流水号: str
    重量kg: Decimal
    毛重kg: Decimal | None
    皮重kg: Decimal | None
    过磅时间: str
    断网离线: bool
    设备号: str
    同步时间: str
    检验编号: str | None = None
    结算编号: str | None = None


@dataclass
class Inspection:
    编号: str
    磅单编号: str
    批次编号: str
    检验时间: str
    检验员: str
    等级: str
    糖度: Decimal | None
    判定依据: str
    样本编号: str
    样本封存位置: str
    来源: str = "初检"             # 初检 / 复核
    复核自: str | None = None
    现行: bool = True


@dataclass
class PriceRule:
    编号: str
    版本: int
    生效时间: str
    保护价: dict                  # 等级 -> 元/kg
    质量系数: dict                # 等级 -> 系数
    市场价参考: dict
    失效时间: str | None = None
    备注: str = ""


@dataclass
class Contract:
    编号: str
    农户编号: str
    企业编号: str
    签约时间: str
    价规编号: str                 # 签约时锁定的价规版本
    等级: list[str]
    季: str


@dataclass
class Delivery:
    编号: str
    企业编号: str
    批次编号: str
    农户编号: str
    合约编号: str
    交货时间: str
    价规编号: str                 # 交货时适用的价规版本
    毛重kg: Decimal
    备注: str = ""


@dataclass
class Settlement:
    编号: str
    农户编号: str
    批次编号: str
    合约编号: str
    交货单编号: str
    时间: str
    行: list[dict] = field(default_factory=list)
    调整编号: list[str] = field(default_factory=list)
    状态: str = "正常"            # 正常 / 含调整


@dataclass
class GradeAdjustment:
    编号: str
    原检验编号: str
    新检验编号: str
    磅单编号: str
    农户编号: str
    原等级: str
    新等级: str
    原单价: str
    新单价: str
    重量kg: Decimal
    差额: str                     # 正=补付，负=扣回
    时间: str
    结算编号: str
    方向: str                     # 补付 / 扣回


@dataclass
class EnterpriseReturn:
    编号: str
    交货单编号: str
    批次编号: str
    重量kg: Decimal
    原因: str
    时间: str
    检验依据编号: str
    处置: str = ""                # 备注，退款金额走企业对账，不进农户结算流水


@dataclass
class TransitLoss:
    编号: str
    交货单编号: str
    批次编号: str
    核定重量kg: Decimal
    到货重量kg: Decimal
    损耗kg: Decimal
    时间: str
    核定人: str
    备注: str = ""


@dataclass
class ProcessingRoute:
    编号: str
    来源类型: str                 # 退货 / 损耗
    来源单号: str
    批次编号: str
    制品: str                     # 陈皮 / 果酱 / ...
    投入重量kg: Decimal
    时间: str
    经办人: str


@dataclass
class Violation:
    编号: str
    树群编号: str
    地块编号: str | None
    类型: str                     # 病害 / 违规采摘
    描述: str
    上报人: str
    时间: str
    处理状态: str = "待处理"


# ---------------------------------------------------------------------------
# 加工与召回
# ---------------------------------------------------------------------------

# 召回处置六类动作
ACTION_QUARANTINE = "隔离"       # 在库货物就地封存
ACTION_INTERCEPT = "拦截"        # 在途货物拦停
ACTION_RETURN = "退回"           # 退回上一保管方
ACTION_DESTROY = "销毁"          # 监督销毁
ACTION_RELEASE = "解除"          # 解除控制（含确认未受影响）
ACTION_REPLACE = "替代交付"      # 以合格货替代交付

DISPOSE_ACTIONS = {ACTION_QUARANTINE, ACTION_INTERCEPT, ACTION_RETURN,
                   ACTION_DESTROY, ACTION_RELEASE, ACTION_REPLACE}
# 实际消耗/转移受控数量、需要参与守恒勾稽的动作
CONSUMING_ACTIONS = {ACTION_RETURN, ACTION_DESTROY, ACTION_REPLACE}

# 资金追加账类型
FUND_HOLD = "暂缓"               # 暂缓支付
FUND_CLAIM_BACK = "追回"         # 已付款追回
FUND_RESTORE = "恢复"            # 暂缓解除、恢复支付

# 召回可发起的节点类型
NODE_PLOT = "地块"
NODE_TICKET = "过磅批次"
NODE_PROCESS_BATCH = "加工批次"
NODE_PRODUCT = "成品"
RECALL_NODE_TYPES = {NODE_PLOT, NODE_TICKET, NODE_PROCESS_BATCH, NODE_PRODUCT}

# 任务生命周期
TASK_PENDING = "待通知"
TASK_NOTIFIED = "待回执"
TASK_DONE = "已处置"
TASK_DISPUTED = "冲突待复核"


@dataclass
class ProcessingBatch:
    编号: str
    加工方: str
    时间: str
    制品: str
    投入明细: list[dict] = field(default_factory=list)  # [{来源类型,来源单号,重量kg}]
    投入合计kg: Decimal = Decimal("0")


@dataclass
class FinishedGood:
    编号: str
    加工批次编号: str
    名称: str
    批次号: str
    重量kg: Decimal
    时间: str
    当前保管方: str                # actor.name：加工方/企业/...
    保管方角色: str
    地点: str = ""
    状态: str = "在库"             # 在库/在途/已替代/已销毁


@dataclass
class RecallScopeVersion:
    版本: int
    时间: str
    操作: str                      # 建立/扩大/缩小
    节点: list[str]                # 该版本完整范围内的货权节点编号
    说明: str = ""


@dataclass
class RecallTask:
    编号: str
    召回编号: str
    节点类型: str                  # 过磅批次/加工批次/成品
    节点编号: str
    保管方: str                    # 农户编号 / 企业名 / 加工方名
    保管方角色: str
    动作: str
    受控数量kg: Decimal            # 该节点受当前范围影响的数量（按父子数量关系摊算）
    状态: str = TASK_PENDING
    加入版本: int = 1
    移出版本: int | None = None
    人工隔离: bool = False         # 人工隔离后，缩范围不自动释放
    已处置kg: Decimal = Decimal("0")
    处置去向: dict = field(default_factory=dict)  # 动作 -> 累计kg
    关闭版本: int | None = None
    保管方已回执: bool = False
    授权解除: bool = False
    备注: str = ""


@dataclass
class RecallNotice:
    编号: str
    召回编号: str
    任务编号: str
    渠道: str
    时间: str
    内容指纹: str


@dataclass
class RecallReceipt:
    编号: str
    召回编号: str
    任务编号: str
    回执号: str                    # 离线凭证号/报文号，幂等键
    保管方: str
    动作: str
    数量kg: Decimal
    时间: str
    离线: bool
    重复: bool = False
    首次通知编号: str | None = None


@dataclass
class RecallConflict:
    编号: str
    召回编号: str
    任务编号: str
    保管方: str
    上报数量kg: Decimal
    系统数量kg: Decimal
    原因: str
    时间: str
    状态: str = "待授权"           # 待授权/已授权维持/已授权调整/已驳回
    授权人: str | None = None
    授权时间: str | None = None
    授权意见: str = ""
    认定数量kg: Decimal | None = None


@dataclass
class RecallFundEntry:
    编号: str
    召回编号: str
    类型: str                      # 暂缓/追回/恢复
    结算编号: str
    农户编号: str
    磅单编号: str
    重量kg: Decimal
    金额: str
    时间: str
    依据: str
    关联编号: str | None = None    # 恢复关联原暂缓/追回
    操作人: str = ""


@dataclass
class Recall:
    编号: str
    发起节点类型: str
    发起节点编号: str
    原因: str
    检验依据编号: str
    发起人: str
    发起时间: str
    当前版本: int = 0
    版本历史: list[RecallScopeVersion] = field(default_factory=list)
    范围内节点: set[str] = field(default_factory=set)        # 货权节点编号（磅单/加工批次/成品）
    状态: str = "处置中"           # 处置中/已结案
    结案时间: str | None = None
    结案报告: dict | None = None
    确认未受影响: bool = False


# ---------------------------------------------------------------------------
# 领域服务
# ---------------------------------------------------------------------------


class HeritageCitrusService:
    """线程安全的内存领域服务（持久化可在序列面层替换存储）。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._actors: dict[str, Actor] = {}

        self._plots: dict[str, Plot] = {}
        self._trees: dict[str, TreeGroup] = {}
        self._cares: deque[CareLog] = deque()
        self._batches: dict[str, HarvestBatch] = {}
        self._tickets: dict[str, WeighTicket] = {}
        self._inspections: dict[str, Inspection] = {}
        self._rules: dict[str, PriceRule] = {}
        self._rule_versions: deque[str] = deque()
        self._contracts: dict[str, Contract] = {}
        self._deliveries: dict[str, Delivery] = {}
        self._settlements: dict[str, Settlement] = {}
        self._adjustments: dict[str, GradeAdjustment] = {}
        self._returns: dict[str, EnterpriseReturn] = {}
        self._losses: dict[str, TransitLoss] = {}
        self._routes: dict[str, ProcessingRoute] = {}
        self._violations: dict[str, Violation] = {}

        # 加工与召回
        self._process_batches: dict[str, ProcessingBatch] = {}
        self._goods: dict[str, FinishedGood] = {}
        self._recalls: dict[str, Recall] = {}
        self._recall_tasks: dict[str, RecallTask] = {}
        self._recall_notices: dict[str, RecallNotice] = {}
        self._recall_receipts: dict[str, RecallReceipt] = {}
        self._recall_conflicts: dict[str, RecallConflict] = {}
        self._recall_funds: dict[str, RecallFundEntry] = {}
        # 已暂缓/追回资金占用：结算编号 -> 磅单编号 -> 累计占用重量
        self._fund_holds: dict[str, dict[str, Decimal]] = {}

        # 配额：树群 -> 季 -> 限额
        self._quotas: dict[tuple[str, str], dict] = {}
        # 保护树预占：批次 -> 树群 -> 重量
        self._reservations: dict[str, dict[str, Decimal]] = {}

        self._seq = 0
        self._farmer_of_plot: dict[str, str] = {}

    # ------------------------------------------------------------------ 工具

    def _id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:06d}"

    @staticmethod
    def _dec(value, label: str) -> Decimal:
        try:
            d = Decimal(str(value))
        except Exception:
            raise ValidationFailed(f"{label}必须是数字")
        if d < 0:
            raise ValidationFailed(f"{label}不能为负")
        return d

    def _require_role(self, actor: Actor, roles: set[str], what: str):
        if actor.role not in roles:
            raise PermissionDenied(what)

    # ------------------------------------------------------------------ 身份

    def register_actor(self, token: str, name: str, role: str, farmer_id: str | None = None) -> dict:
        with self._lock:
            if role not in {ROLE_FARMER, ROLE_COOP, ROLE_REVIEWER, ROLE_GUARD,
                            ROLE_ENTERPRISE, ROLE_PROCESSOR, ROLE_REGULATOR}:
                raise ValidationFailed("未知角色")
            if role == ROLE_FARMER and not farmer_id:
                raise ValidationFailed("果农身份必须绑定农户编号")
            self._actors[token] = Actor(token=token, name=name, role=role, farmer_id=farmer_id)
            return {"令牌": token, "姓名": name, "角色": role, "农户编号": farmer_id}

    def authenticate(self, token: str | None) -> Actor:
        if not token:
            raise AuthError()
        actor = self._actors.get(token)
        if actor is None:
            raise AuthError()
        return actor

    def _farmer_scope(self, actor: Actor, farmer_id: str):
        """果农只能操作/查看本人档案；护树队根本不在此列。"""
        if actor.role == ROLE_FARMER and actor.farmer_id != farmer_id:
            raise PermissionDenied("访问他人农户档案")
        if actor.role == ROLE_GUARD:
            raise PermissionDenied("访问农户业务档案")

    # ------------------------------------------------------------------ 基础建档

    def register_farmer(self, actor: Actor, name: str) -> dict:
        self._require_role(actor, {ROLE_COOP}, "登记农户")
        with self._lock:
            fid = self._id("农户")
            return {"农户编号": fid, "姓名": name}

    def create_plot(self, actor: Actor, farmer_id: str, name: str, location: str) -> dict:
        self._require_role(actor, {ROLE_COOP, ROLE_FARMER}, "建档地块")
        self._farmer_scope(actor, farmer_id)
        with self._lock:
            pid = self._id("地块")
            self._plots[pid] = Plot(pid, farmer_id, name, location)
            self._farmer_of_plot[pid] = farmer_id
            return self._plot_view(self._plots[pid])

    def register_tree_group(self, actor: Actor, plot_id: str, name: str, species: str,
                            age_years: int, protection: str) -> dict:
        self._require_role(actor, {ROLE_COOP, ROLE_FARMER}, "登记树群")
        with self._lock:
            plot = self._plots.get(plot_id)
            if plot is None:
                raise NotFound("地块")
            self._farmer_scope(actor, plot.农户编号)
            if protection not in {"普通老树", "百年保护树"}:
                raise ValidationFailed("保护级别必须是 普通老树 / 百年保护树")
            tid = self._id("树群")
            self._trees[tid] = TreeGroup(tid, plot_id, name, species, int(age_years), protection)
            return self._tree_view(self._trees[tid], actor)

    def set_quota(self, actor: Actor, tree_id: str, season: str, weight_kg, times: int) -> dict:
        """合作社对保护树群按采收季下达配额（百年树必须留种，配额从严）。"""
        self._require_role(actor, {ROLE_COOP}, "下达保护树配额")
        with self._lock:
            tree = self._trees.get(tree_id)
            if tree is None:
                raise NotFound("树群")
            if tree.保护级别 != "百年保护树":
                raise ValidationFailed("配额仅对百年保护树设置")
            weight = self._dec(weight_kg, "配额重量")
            if times < 0:
                raise ValidationFailed("配额次数不能为负")
            self._quotas[(tree_id, season)] = {"重量": weight, "次数": int(times)}
            return {"树群编号": tree_id, "季": season, "配额重量kg": str(weight), "配额次数": int(times)}

    def add_care_log(self, actor: Actor, tree_id: str, date: str, item: str,
                     disease: bool = False, disease_desc: str = "") -> dict:
        """管护记录。护树队可登记病害；日常管护由合作社/果农记录。"""
        allowed = {ROLE_COOP, ROLE_FARMER, ROLE_GUARD}
        self._require_role(actor, allowed, "记录管护信息")
        with self._lock:
            tree = self._trees.get(tree_id)
            if tree is None:
                raise NotFound("树群")
            if actor.role == ROLE_FARMER:
                self._farmer_scope(actor, self._farmer_of_plot[tree.地块编号])
            if disease:
                tree.健康状态 = "染病"
            log = CareLog(self._id("管护"), tree_id, date, item, actor.name,
                          disease, disease_desc)
            self._cares.append(log)
            return self._care_view(log)

    # ------------------------------------------------------------------ 采收与过磅

    def open_batch(self, actor: Actor, plot_id: str, harvest_date: str, season: str) -> dict:
        self._require_role(actor, {ROLE_COOP, ROLE_FARMER}, "开立采收批次")
        with self._lock:
            plot = self._plots.get(plot_id)
            if plot is None:
                raise NotFound("地块")
            self._farmer_scope(actor, plot.农户编号)
            bid = self._id("批次")
            self._batches[bid] = HarvestBatch(bid, plot_id, plot.农户编号, harvest_date, season)
            return self._batch_view(self._batches[bid])

    def reserve_trees(self, actor: Actor, batch_id: str, tree_id: str, weight_kg) -> dict:
        """采收前对保护树群预占额度（普通老树无需预占）。

        预占按树群-季汇总核校验配额，是“保护树采收不得越界”的第一道闸。
        """
        self._require_role(actor, {ROLE_COOP, ROLE_FARMER}, "预占保护树采收额度")
        weight = self._dec(weight_kg, "预占重量")
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                raise NotFound("采收批次")
            self._farmer_scope(actor, batch.农户编号)
            tree = self._trees.get(tree_id)
            if tree is None:
                raise NotFound("树群")
            if self._farmer_of_plot[tree.地块编号] != batch.农户编号:
                raise ValidationFailed("树群不属于该批次所属农户的地块")
            if tree.保护级别 != "百年保护树":
                raise ValidationFailed("普通老树无需预占")
            quota = self._quotas.get((tree_id, batch.季))
            if quota is None:
                raise Conflict("quota_not_set", "该保护树群本采收季尚未下达配额，不得采收")
            # 该树群本季总预占 = 其他批次预占 + 本批次既有预占 + 本次预占
            batch_res = self._reservations.setdefault(batch_id, {})
            already = batch_res.get(tree_id, Decimal("0"))
            # 该树群本季总预占 = 其他批次预占 + 本批次新值
            other = Decimal("0")
            for b in self._batches.values():
                if b.编号 == batch_id or b.季 != batch.季:
                    continue
                other += self._reservations.get(b.编号, {}).get(tree_id, Decimal("0"))
            total = other + already + weight
            if total > quota["重量"]:
                raise Conflict(
                    "quota_exceeded",
                    f"预占越界：{tree.名称} 本季配额 {quota['重量']}kg，"
                    f"已有预占 {other + already}kg，本次 {weight}kg",
                )
            if weight > 0:
                batch_res[tree_id] = already + weight
                batch.预占重量 += weight
                if tree_id not in batch.来源树群:
                    batch.来源树群.append(tree_id)
            return {"批次编号": batch_id, "树群编号": tree_id,
                    "本次预占kg": str(weight), "累计预占kg": str(batch_res[tree_id])}

    def weigh(self, actor: Actor, batch_id: str, tree_id: str, slip_no: str,
              weight_kg=None, gross_kg=None, tare_kg=None, offline=False,
              device: str = "", at: str | None = None) -> dict:
        """登记过磅单。

        * ``(批次, 过磅流水号)`` 幂等：断网设备恢复后重传同一张纸单/同一流水号，
          返回原记录而不是第二次入账——这是“同一批果不重复结算”的根。
        * 保护树群的磅单回冲本批次预占，超出预占即拒绝（现场超采立即暴露）。
        """
        self._require_role(actor, {ROLE_COOP, ROLE_FARMER}, "过磅登记")
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                raise NotFound("采收批次")
            self._farmer_scope(actor, batch.农户编号)
            if batch.状态 == "已交货":
                raise Conflict("batch_delivered", "批次已交货，不能追加过磅")
            tree = self._trees.get(tree_id)
            if tree is None:
                raise NotFound("树群")
            if self._farmer_of_plot[tree.地块编号] != batch.农户编号:
                raise ValidationFailed("树群不属于该批次所属农户的地块")

            # 幂等：同批次同流水号只认第一次
            for t in self._tickets.values():
                if t.批次编号 == batch_id and t.过磅流水号 == slip_no:
                    return self._ticket_view(t, actor, duplicated=True)

            if weight_kg is not None:
                weight = self._dec(weight_kg, "净重")
                gross = self._dec(gross_kg, "毛重") if gross_kg is not None else None
                tare = self._dec(tare_kg, "皮重") if tare_kg is not None else None
            else:
                gross = self._dec(gross_kg, "毛重")
                tare = self._dec(tare_kg, "皮重")
                weight = gross - tare
                if weight < 0:
                    raise ValidationFailed("皮重不能大于毛重")
            if weight <= 0:
                raise ValidationFailed("过磅净重必须大于零")

            # 保护树：回冲预占 + 配额次数
            if tree.保护级别 == "百年保护树":
                quota = self._quotas.get((tree_id, batch.季))
                if quota is None:
                    raise Conflict("quota_not_set", "该保护树群本采收季尚未下达配额，不得采收")
                batch_res = self._reservations.get(batch_id, {})
                reserved = batch_res.get(tree_id, Decimal("0"))
                used = sum(
                    tk.重量kg for tk in self._tickets.values()
                    if tk.批次编号 == batch_id and tk.树群编号 == tree_id
                )
                if used + weight > reserved:
                    raise Conflict(
                        "over_reservation",
                        f"超量采摘：{tree.名称} 本批次预占 {reserved}kg，"
                        f"已过磅 {used}kg，本次 {weight}kg 超出额度",
                    )
                times_used = sum(
                    1 for tk in self._tickets.values()
                    if tk.树群编号 == tree_id
                    and self._batches[tk.批次编号].季 == batch.季
                )
                if times_used + 1 > quota["次数"]:
                    raise Conflict("quota_times_exceeded",
                                   f"{tree.名称} 本季采摘次数超过配额 {quota['次数']} 次")

            tid = self._id("磅单")
            ticket = WeighTicket(
                编号=tid, 批次编号=batch_id, 树群编号=tree_id, 过磅流水号=slip_no,
                重量kg=weight, 毛重kg=gross, 皮重kg=tare,
                过磅时间=at or _now(), 断网离线=bool(offline), 设备号=device,
                同步时间=_now(),
            )
            self._tickets[tid] = ticket
            tree.本季已采重量 += weight
            tree.本季已采次数 += 1
            if tree_id not in batch.来源树群:
                batch.来源树群.append(tree_id)
            if batch.状态 == "采收中":
                batch.状态 = "待检验"
            return self._ticket_view(ticket, actor)

    # ------------------------------------------------------------------ 检验与复核

    def inspect(self, actor: Actor, ticket_id: str, grade: str, basis: str,
                brix=None, sample_location: str = "合作社留样柜") -> dict:
        """初检：取样、定级。样本必须封存，复核时据此比对。"""
        self._require_role(actor, {ROLE_COOP, ROLE_REVIEWER}, "果品检验")
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise NotFound("磅单")
            batch = self._batches[ticket.批次编号]
            if ticket.检验编号 is not None:
                raise Conflict("already_inspected", "该磅单已完成初检")
            iid = self._id("检验")
            sid = self._id("样本")
            insp = Inspection(
                编号=iid, 磅单编号=ticket_id, 批次编号=batch.编号,
                检验时间=_now(), 检验员=actor.name, 等级=grade,
                糖度=Decimal(str(brix)) if brix is not None else None,
                判定依据=basis, 样本编号=sid, 样本封存位置=sample_location,
            )
            self._inspections[iid] = insp
            ticket.检验编号 = iid
            if batch.状态 == "待检验":
                batch.状态 = "待收购"
            return self._inspection_view(insp)

    def review_grade(self, actor: Actor, inspection_id: str, new_grade: str,
                     new_basis: str, brix=None) -> dict:
        """质量复核改级。

        原检验记录与原样本完整保留（``现行=False`` 但不删除），新记录标明“复核自”，
        等级变化另走价差调整流水；尚未结算的磅单按新等级结算，不产生调整。
        """
        self._require_role(actor, {ROLE_REVIEWER, ROLE_COOP}, "质量复核")
        with self._lock:
            old = self._inspections.get(inspection_id)
            if old is None:
                raise NotFound("检验记录")
            if not old.现行:
                raise Conflict("superseded", "该检验已被后续复核取代，请对最新记录复核")
            ticket = self._tickets[old.磅单编号]
            old.现行 = False
            iid = self._id("检验")
            new = Inspection(
                编号=iid, 磅单编号=ticket.编号, 批次编号=old.批次编号,
                检验时间=_now(), 检验员=actor.name, 等级=new_grade,
                糖度=Decimal(str(brix)) if brix is not None else old.糖度,
                判定依据=new_basis, 样本编号=old.样本编号,
                样本封存位置=old.样本封存位置, 来源="复核", 复核自=old.编号,
            )
            self._inspections[iid] = new
            ticket.检验编号 = iid

            adjustment = None
            if ticket.结算编号 is not None:
                settlement = self._settlements[ticket.结算编号]
                adjustment = self._apply_grade_adjustment(actor, settlement, ticket, old, new)
            return {"新检验": self._inspection_view(new),
                    "原检验": self._inspection_view(old),
                    "价差调整": self._adjustment_view(adjustment) if adjustment else None}

    def _apply_grade_adjustment(self, actor: Actor, settlement: Settlement,
                                ticket: WeighTicket, old: Inspection, new: Inspection):
        delivery = self._deliveries[settlement.交货单编号]
        old_price = self._grade_unit_price(delivery.价规编号, old.等级)
        new_price = self._grade_unit_price(delivery.价规编号, new.等级)
        diff = (new_price - old_price) * ticket.重量kg
        aid = self._id("价差")
        adj = GradeAdjustment(
            编号=aid, 原检验编号=old.编号, 新检验编号=new.编号,
            磅单编号=ticket.编号, 农户编号=settlement.农户编号,
            原等级=old.等级, 新等级=new.等级,
            原单价=_money(old_price), 新单价=_money(new_price),
            重量kg=ticket.重量kg, 差额=_money(diff), 时间=_now(),
            结算编号=settlement.编号, 方向="补付" if diff >= 0 else "扣回",
        )
        self._adjustments[aid] = adj
        settlement.调整编号.append(aid)
        settlement.状态 = "含调整"
        return adj

    # ------------------------------------------------------------------ 价规与合约

    def publish_price_rule(self, actor: Actor, effective_at: str, protected: dict,
                           coefficients: dict | None = None, market_reference: dict | None = None,
                           note: str = "") -> dict:
        """发布新价规版本。历史版本永不修改——后续市场价只能另出新版。"""
        self._require_role(actor, {ROLE_COOP}, "发布价规")
        with self._lock:
            version = len(self._rule_versions) + 1
            rid = f"价规-v{version}"
            rule = PriceRule(
                编号=rid, 版本=version, 生效时间=effective_at,
                保护价={g: Decimal(str(p)) for g, p in protected.items()},
                质量系数={g: Decimal(str(c)) for g, c in (coefficients or {}).items()} or
                         {g: Decimal("1") for g in protected},
                市场价参考={g: Decimal(str(p)) for g, p in (market_reference or {}).items()},
                备注=note,
            )
            self._rules[rid] = rule
            self._rule_versions.append(rid)
            return self._rule_view(rule)

    def sign_contract(self, actor: Actor, farmer_id: str, enterprise_id: str,
                      signed_at: str, grades: list[str], season: str) -> dict:
        """签约：记录签约时刻的价规版本，作为该合约的基准规则。"""
        self._require_role(actor, {ROLE_COOP}, "签订收购合约")
        with self._lock:
            if not self._rule_versions:
                raise Conflict("no_price_rule", "尚未发布任何价规，无法签约")
            base_rule = self._rule_at(signed_at)
            cid = self._id("合约")
            contract = Contract(cid, farmer_id, enterprise_id, signed_at,
                                base_rule.编号, list(grades), season)
            self._contracts[cid] = contract
            return self._contract_view(contract)

    def _rule_at(self, timestamp: str) -> PriceRule:
        """返回指定时刻适用（已生效）的最新价规。"""
        current = None
        for rid in self._rule_versions:
            rule = self._rules[rid]
            if rule.生效时间 <= timestamp:
                current = rule
        if current is None:
            raise Conflict("no_applicable_rule", f"{timestamp} 前没有已生效的价规")
        return current

    def _grade_unit_price(self, rule_id: str, grade: str) -> Decimal:
        rule = self._rules[rule_id]
        if grade not in rule.保护价:
            raise ValidationFailed(f"价规 {rule_id} 未覆盖等级「{grade}」")
        return rule.保护价[grade] * rule.质量系数.get(grade, Decimal("1"))

    # ------------------------------------------------------------------ 交货与结算

    def deliver(self, actor: Actor, batch_id: str, enterprise_id: str,
                contract_id: str, delivered_at: str) -> dict:
        """整批交货：锁定交货时适用价规版本，随后按磅单结算。"""
        self._require_role(actor, {ROLE_COOP, ROLE_ENTERPRISE}, "登记交货")
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                raise NotFound("采收批次")
            if actor.role == ROLE_ENTERPRISE and enterprise_id != actor.name:
                # 企业编号与身份的弱校验；合作社代办不受限
                pass
            contract = self._contracts.get(contract_id)
            if contract is None:
                raise NotFound("收购合约")
            if contract.农户编号 != batch.农户编号:
                raise ValidationFailed("合约与批次不属于同一农户")
            if contract.企业编号 != enterprise_id:
                raise ValidationFailed("合约收购企业与交货企业不一致")
            if batch.状态 == "已交货":
                raise Conflict("already_delivered", "该批次已交货")
            tickets = [t for t in self._tickets.values() if t.批次编号 == batch_id]
            if not tickets:
                raise Conflict("empty_batch", "空批次不能交货")
            unspected = [t.编号 for t in tickets if t.检验编号 is None]
            if unspected:
                raise Conflict("inspection_pending", f"尚有磅单未检验：{unspected}")

            applicable = self._rule_at(delivered_at)
            did = self._id("交货")
            gross = sum(t.重量kg for t in tickets)
            delivery = Delivery(did, enterprise_id, batch_id, batch.农户编号,
                                contract_id, delivered_at, applicable.编号, gross)
            self._deliveries[did] = delivery
            batch.状态 = "已交货"
            batch.交货单编号 = did
            return self._delivery_view(delivery)

    def settle(self, actor: Actor, delivery_id: str) -> dict:
        """按 *签约规则 + 交货时适用价规* 为交货批次逐磅单结算。

        已结算磅单不可再次结算；结算金额在此时固化，之后任何价规新版本都不影响它。
        """
        self._require_role(actor, SETTLER_ROLES, "办理农户结算")
        with self._lock:
            delivery = self._deliveries.get(delivery_id)
            if delivery is None:
                raise NotFound("交货单")
            batch = self._batches[delivery.批次编号]

            tickets = [t for t in self._tickets.values() if t.批次编号 == batch.编号]
            fresh = [t for t in tickets if t.结算编号 is None]
            if not fresh:
                settled = [t.结算编号 for t in tickets]
                raise Conflict("already_settled",
                               f"该交货批次全部磅单已结算（{sorted(set(settled))}），每公斤果实只能结算一次")

            # 召回闸门：仍处于召回处置中的磅单不得结算（暂缓支付在召回侧以追加账体现）
            blocked = sorted({
                t.过磅流水号 for t in fresh
                if self._ticket_under_active_recall(t.编号)
            })
            if blocked:
                raise Conflict(
                    "recall_hold",
                    f"以下过磅批次处于农残召回处置中，暂缓结算，待解除后恢复：{blocked}")

            sid = self._id("结算")
            settlement = Settlement(
                编号=sid, 农户编号=delivery.农户编号, 批次编号=batch.编号,
                合约编号=delivery.合约编号, 交货单编号=delivery_id, 时间=_now(),
            )
            total = Decimal("0")
            for t in sorted(fresh, key=lambda x: x.编号):
                insp = self._inspections[t.检验编号]
                unit = self._grade_unit_price(delivery.价规编号, insp.等级)
                amount = unit * t.重量kg
                total += amount
                t.结算编号 = sid
                settlement.行.append({
                    "磅单编号": t.编号,
                    "过磅流水号": t.过磅流水号,
                    "树群编号": t.树群编号,
                    "重量kg": str(t.重量kg),
                    "等级": insp.等级,
                    "检验编号": insp.编号,
                    "单价": _money(unit),
                    "金额": _money(amount),
                })
            settlement.行.append({"合计应收": _money(total),
                                  "计价价规": delivery.价规编号,
                                  "签约价规": self._contracts[delivery.合约编号].价规编号})
            self._settlements[sid] = settlement
            return self._settlement_view(settlement)

    # ------------------------------------------------------------------ 三条后置独立流水

    def enterprise_return(self, actor: Actor, delivery_id: str, weight_kg,
                          reason: str, inspection_id: str, disposal: str = "") -> dict:
        """企业退货：独立流水。退货不冲减农户已结算应收（价差另走质量复核/合约争议）。

        数量勾稽：退货只能来自实际到货果实，故累计退货不得超过交货量；
        若运输损耗已核定，退货量还不得超过到货量。
        """
        self._require_role(actor, {ROLE_COOP, ROLE_ENTERPRISE}, "登记企业退货")
        weight = self._dec(weight_kg, "退货重量")
        with self._lock:
            delivery = self._deliveries.get(delivery_id)
            if delivery is None:
                raise NotFound("交货单")
            if inspection_id not in self._inspections:
                raise NotFound("退货检验依据")
            returned = sum(r.重量kg for r in self._returns.values()
                           if r.交货单编号 == delivery_id)
            if returned + weight > delivery.毛重kg:
                raise ValidationFailed(
                    f"退货累计 {returned + weight}kg 超过交货量 {delivery.毛重kg}kg")
            losses = [r for r in self._losses.values() if r.交货单编号 == delivery_id]
            if losses and returned + weight > losses[0].到货重量kg:
                raise ValidationFailed(
                    f"退货累计 {returned + weight}kg 超过实际到货量 "
                    f"{losses[0].到货重量kg}kg（退货只能来自到货果实）")
            rid = self._id("退货")
            rec = EnterpriseReturn(rid, delivery_id, delivery.批次编号, weight,
                                   reason, _now(), inspection_id, disposal)
            self._returns[rid] = rec
            return self._return_view(rec)

    def transit_loss(self, actor: Actor, delivery_id: str, arrived_kg, note: str = "") -> dict:
        """运输损耗：以交货核定毛重与到货过磅差额独立入账（每交货单核定一次）。"""
        self._require_role(actor, {ROLE_COOP, ROLE_ENTERPRISE}, "核定运输损耗")
        arrived = self._dec(arrived_kg, "到货重量")
        with self._lock:
            delivery = self._deliveries.get(delivery_id)
            if delivery is None:
                raise NotFound("交货单")
            if any(r.交货单编号 == delivery_id for r in self._losses.values()):
                raise Conflict("loss_already_recorded", "该交货单已核定过运输损耗")
            if arrived > delivery.毛重kg:
                raise ValidationFailed("到货重量不能大于交货核定重量")
            returned = sum(r.重量kg for r in self._returns.values()
                           if r.交货单编号 == delivery_id)
            if arrived < returned:
                raise ValidationFailed(
                    f"到货量 {arrived}kg 小于已登记退货 {returned}kg，数量无法勾稽")
            loss = delivery.毛重kg - arrived
            lid = self._id("损耗")
            rec = TransitLoss(lid, delivery_id, delivery.批次编号,
                              delivery.毛重kg, arrived, loss, _now(), actor.name, note)
            self._losses[lid] = rec
            return self._loss_view(rec)

    def route_to_processing(self, actor: Actor, source_type: str, source_id: str,
                            product: str, input_kg, handler: str = "") -> dict:
        """果肉转入加工路线（陈皮等）：只接受退货/损耗果实，独立流水，可回溯来源。"""
        self._require_role(actor, {ROLE_COOP}, "登记加工路线")
        amount = self._dec(input_kg, "投入重量")
        with self._lock:
            if source_type == "退货":
                src = self._returns.get(source_id)
            elif source_type == "损耗":
                src = self._losses.get(source_id)
            else:
                raise ValidationFailed("加工来源类型必须是 退货 / 损耗")
            if src is None:
                raise NotFound("加工来源单据")
            used = sum(r.投入重量kg for r in self._routes.values()
                       if r.来源类型 == source_type and r.来源单号 == source_id)
            if used + amount > src.重量kg:
                raise ValidationFailed(
                    f"加工投入 {used + amount}kg 超过该来源可处置量 {src.重量kg}kg")
            pid = self._id("加工")
            rec = ProcessingRoute(pid, source_type, source_id, src.批次编号,
                                  product, amount, _now(), handler or actor.name)
            self._routes[pid] = rec
            return self._route_view(rec)

    # ------------------------------------------------------------------ 护树队上报

    def report_tree_issue(self, actor: Actor, tree_id: str, kind: str,
                          description: str) -> dict:
        """护树队职责入口：上报病害或违规采摘。仅此而已——无结算读取权。"""
        self._require_role(actor, {ROLE_GUARD, ROLE_COOP}, "上报树体问题")
        if kind not in {"病害", "违规采摘"}:
            raise ValidationFailed("上报类型必须是 病害 / 违规采摘")
        with self._lock:
            tree = self._trees.get(tree_id)
            if tree is None:
                raise NotFound("树群")
            vid = self._id("巡护")
            rec = Violation(vid, tree_id, tree.地块编号, kind, description,
                            actor.name, _now())
            self._violations[vid] = rec
            if kind == "病害":
                tree.健康状态 = "染病"
            return self._violation_view(rec)

    # ------------------------------------------------------------------ 查询与反查

    def get_tree_group(self, actor: Actor, tree_id: str) -> dict:
        with self._lock:
            tree = self._trees.get(tree_id)
            if tree is None:
                raise NotFound("树群")
            if actor.role == ROLE_FARMER:
                self._farmer_scope(actor, self._farmer_of_plot[tree.地块编号])
            return self._tree_view(tree, actor)

    def list_batches(self, actor: Actor) -> list[dict]:
        with self._lock:
            out = []
            for b in self._batches.values():
                if actor.role == ROLE_FARMER and b.农户编号 != actor.farmer_id:
                    continue
                if actor.role == ROLE_GUARD:
                    raise PermissionDenied("查看农户采收批次")
                out.append(self._batch_view(b))
            return out

    def get_batch_detail(self, actor: Actor, batch_id: str) -> dict:
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                raise NotFound("采收批次")
            self._farmer_scope(actor, batch.农户编号)
            tickets = [t for t in self._tickets.values() if t.批次编号 == batch_id]
            return {
                **self._batch_view(batch),
                "磅单": [self._ticket_view(t, actor) for t in tickets],
                "来源树群": batch.来源树群,
            }

    def get_settlement(self, actor: Actor, settlement_id: str) -> dict:
        with self._lock:
            settlement = self._settlements.get(settlement_id)
            if settlement is None:
                raise NotFound("结算单")
            self._farmer_scope(actor, settlement.农户编号)
            if actor.role not in SETTLE_DETAIL_VIEWERS and actor.role != ROLE_FARMER:
                raise PermissionDenied("查看农户结算明细")
            return self._settlement_view(settlement)

    def list_ledger(self, actor: Actor, ledger: str) -> dict:
        """四条独立流水的账本导出。护树队对任何结算性账本均不可见。"""
        names = {
            "农户结算": (self._settlements, self._settlement_view),
            "企业退货": (self._returns, self._return_view),
            "运输损耗": (self._losses, self._loss_view),
            "果肉加工": (self._routes, self._route_view),
        }
        if ledger not in names:
            raise ValidationFailed("账本必须是 农户结算/企业退货/运输损耗/果肉加工")
        if actor.role == ROLE_GUARD:
            raise PermissionDenied("查看结算与货物流水")
        store, viewer = names[ledger]
        with self._lock:
            rows = []
            for rec in store.values():
                fid = getattr(rec, "农户编号", None)
                if actor.role == ROLE_FARMER and fid is not None and fid != actor.farmer_id:
                    continue
                if actor.role == ROLE_FARMER and ledger != "农户结算":
                    # 退货/损耗/加工是企业与合作社侧账，不向农户开放明细
                    continue
                rows.append(viewer(rec))
            return {"账本": ledger, "条数": len(rows), "记录": rows}

    def trace_from_product(self, actor: Actor, route_id: str = "", delivery_id: str = "") -> dict:
        """成品原料反查：从交货单（或其退货/加工去向）一路回到地块、检测依据、受益农户。

        企业可用此接口自证原料来源；护树队无权使用（会暴露农户信息）。
        """
        if actor.role == ROLE_GUARD:
            raise PermissionDenied("反查成品原料来源")
        with self._lock:
            if route_id:
                route = self._routes.get(route_id)
                if route is None:
                    raise NotFound("加工路线单")
                delivery_id = self._delivery_of_source(route.来源类型, route.来源单号)
                chain_route = self._route_view(route)
            else:
                chain_route = None

            delivery = self._deliveries.get(delivery_id)
            if delivery is None:
                raise NotFound("交货单")
            if actor.role == ROLE_FARMER and delivery.农户编号 != actor.farmer_id:
                raise PermissionDenied("反查他人交货批次")
            if actor.role == ROLE_ENTERPRISE and delivery.企业编号 != actor.name:
                raise PermissionDenied("反查非本企业交货单")

            batch = self._batches[delivery.批次编号]
            plot = self._plots[batch.地块编号]
            tickets = [t for t in self._tickets.values() if t.批次编号 == batch.编号]
            tree_ids = sorted({t.树群编号 for t in tickets})
            chain = {
                "交货单": self._delivery_view(delivery),
                "受益农户": {"农户编号": batch.农户编号},
                "地块": self._plot_view(plot),
                "采收批次": self._batch_view(batch),
                "树群": [
                    {k: v for k, v in self._tree_view(self._trees[t], actor).items()}
                    for t in tree_ids
                ],
                "管护记录": [
                    self._care_view(c) for c in self._cares
                    if c.树群编号 in tree_ids
                ],
                "磅单与检测依据": [
                    {
                        "磅单": self._ticket_view(t, actor),
                        "现行检验": self._inspection_view(self._inspections[t.检验编号]),
                        "历次检验": [
                            self._inspection_view(i) for i in self._inspections.values()
                            if i.磅单编号 == t.编号
                        ],
                    }
                    for t in sorted(tickets, key=lambda x: x.编号)
                ],
                "企业退货": [self._return_view(r) for r in self._returns.values()
                          if r.交货单编号 == delivery_id],
                "运输损耗": [self._loss_view(r) for r in self._losses.values()
                          if r.交货单编号 == delivery_id],
                "果肉加工": [self._route_view(r) for r in self._routes.values()
                          if r.批次编号 == batch.编号],
            }
            if chain_route:
                chain["加工入口"] = chain_route
            return chain

    def _delivery_of_source(self, source_type: str, source_id: str) -> str:
        if source_type == "退货":
            return self._returns[source_id].交货单编号
        return self._losses[source_id].交货单编号

    # ------------------------------------------------------------------ 加工批次与成品

    def register_processing_batch(self, actor: Actor, product: str,
                                  inputs: list[dict], at: str | None = None) -> dict:
        """加工方建档加工批次：逐笔登记来源（交货/退货/损耗），构成父子数量关系边。

        加工投入不得超过来源可处置量（退货/损耗与既有果肉加工路线共享额度）。
        """
        self._require_role(actor, {ROLE_PROCESSOR, ROLE_COOP}, "建档加工批次")
        with self._lock:
            if not inputs:
                raise ValidationFailed("加工批次必须至少有一笔来源投入")
            total = Decimal("0")
            lines = []
            for line in inputs:
                stype = line.get("来源类型")
                sid = line.get("来源单号")
                amount = self._dec(line.get("重量kg"), "投入重量")
                if amount <= 0:
                    raise ValidationFailed("投入重量必须大于零")
                source_weight, used = self._source_disposable(stype, sid)
                if used + amount > source_weight:
                    raise ValidationFailed(
                        f"加工投入 {used + amount}kg 超过来源 {stype} {sid} "
                        f"可处置量 {source_weight}kg")
                total += amount
                lines.append({"来源类型": stype, "来源单号": sid, "重量kg": amount})
            pid = self._id("加工批")
            rec = ProcessingBatch(pid, actor.name, at or _now(), product, lines, total)
            self._process_batches[pid] = rec
            return self._process_batch_view(rec)

    def _source_disposable(self, stype: str, sid: str) -> tuple[Decimal, Decimal]:
        """返回 (来源总量, 已被加工批次与果肉路线占用的量)。"""
        if stype == "交货":
            src = self._deliveries.get(sid)
        elif stype == "退货":
            src = self._returns.get(sid)
        elif stype == "损耗":
            src = self._losses.get(sid)
        else:
            raise ValidationFailed("加工来源类型必须是 交货 / 退货 / 损耗")
        if src is None:
            raise NotFound("加工来源单据")
        used = sum(
            line["重量kg"] for pb in self._process_batches.values()
            for line in pb.投入明细
            if line["来源类型"] == stype and line["来源单号"] == sid
        )
        if stype in {"退货", "损耗"}:
            used += sum(r.投入重量kg for r in self._routes.values()
                        if r.来源类型 == stype and r.来源单号 == sid)
            return src.重量kg, used
        # 直接用交货果实加工：可处置量 = 核定毛重 − 运输损耗 − 已退货，
        # 损耗与退货各自还能走退货/损耗加工路线，互不重复。
        available = src.毛重kg
        available -= sum(r.重量kg for r in self._returns.values()
                         if r.交货单编号 == sid)
        available -= sum(ls.损耗kg for ls in self._losses.values()
                         if ls.交货单编号 == sid)
        return available, used

    def register_good(self, actor: Actor, process_batch_id: str, name: str,
                      batch_no: str, weight_kg, custodian: str = "",
                      location: str = "", status: str = "在库",
                      at: str | None = None) -> dict:
        """成品入库上报：成品必须挂在加工批次下，成品合计不得超过加工投入量。"""
        self._require_role(actor, {ROLE_PROCESSOR, ROLE_COOP}, "成品上报")
        weight = self._dec(weight_kg, "成品重量")
        if status not in {"在库", "在途"}:
            raise ValidationFailed("上报时成品状态只能是 在库 / 在途")
        with self._lock:
            pb = self._process_batches.get(process_batch_id)
            if pb is None:
                raise NotFound("加工批次")
            custodian_name = custodian or actor.name
            custodian_role = self._custodian_role(custodian_name, actor)
            made = sum(g.重量kg for g in self._goods.values()
                       if g.加工批次编号 == process_batch_id)
            if made + weight > pb.投入合计kg:
                raise ValidationFailed(
                    f"成品合计 {made + weight}kg 超过加工批次投入 {pb.投入合计kg}kg")
            gid = self._id("成品")
            good = FinishedGood(gid, process_batch_id, name, batch_no, weight,
                                at or _now(), custodian_name, custodian_role,
                                location, status)
            self._goods[gid] = good
            return self._good_view(good)

    def _custodian_role(self, custodian_name: str, actor: Actor) -> str:
        """保管方必须是已登记的加工方或收购企业；自报时直接取本人角色。"""
        if custodian_name == actor.name:
            if actor.role not in {ROLE_PROCESSOR, ROLE_ENTERPRISE}:
                raise ValidationFailed("成品保管方只能是加工方或收购企业")
            return actor.role
        for a in self._actors.values():
            if a.name == custodian_name:
                if a.role not in {ROLE_PROCESSOR, ROLE_ENTERPRISE}:
                    raise ValidationFailed("成品保管方只能是加工方或收购企业")
                return a.role
        raise ValidationFailed(f"成品保管方「{custodian_name}」尚未登记为加工方/企业")

    # ------------------------------------------------------------------ 召回发起与范围版本

    def initiate_recall(self, actor: Actor, node_type: str, node_id: str,
                        reason: str, inspection_id: str) -> dict:
        """从 地块/过磅批次/加工批次/成品 任一节点发起召回，形成范围版本 v1。"""
        self._require_role(actor, {ROLE_REGULATOR}, "发起农残召回")
        with self._lock:
            if node_type not in RECALL_NODE_TYPES:
                raise ValidationFailed(
                    "召回发起节点必须是 地块/过磅批次/加工批次/成品")
            if inspection_id not in self._inspections:
                raise NotFound("农残检验依据")
            affected = self._affected_nodes(node_type, node_id)
            if not affected:
                raise ValidationFailed("发起节点向下没有任何可定位的货物，无需召回")
            rid = self._id("召回")
            recall = Recall(rid, node_type, node_id, reason, inspection_id,
                            actor.name, _now())
            self._recalls[rid] = recall
            self._apply_scope(recall, affected, "建立", "首次发起")
            return self._recall_view(recall, actor)

    def _affected_nodes(self, anchor_type: str, anchor_id: str) -> dict[str, dict]:
        """沿父子数量关系计算锚点下游全部 *当前仍在控* 的货权节点。

        同一批货在链上只计一次：已进入加工批次的交货量不再算企业在库，已制成成品
        的投入量留在成品节点（加工批节点只承载尚未成品的在制余量）。
        返回 {节点编号: {类型, 重量kg}}。
        """
        nodes: dict[str, dict] = {}

        def add(node_id: str, ntype: str, weight: Decimal):
            if weight <= 0:
                return
            prev = nodes.get(node_id)
            if prev is None or weight > prev["重量kg"]:
                nodes[node_id] = {"类型": ntype, "重量kg": weight}

        ticket_ids: list[str] = []
        if anchor_type == NODE_PLOT:
            plot = self._plots.get(anchor_id)
            if plot is None:
                raise NotFound("地块")
            ticket_ids = [t.编号 for b in self._batches.values()
                          if b.地块编号 == anchor_id
                          for t in self._tickets.values()
                          if t.批次编号 == b.编号]
        elif anchor_type == NODE_TICKET:
            ticket = self._tickets.get(anchor_id)
            if ticket is None:
                raise NotFound("过磅批次（磅单）")
            ticket_ids = [anchor_id]
        elif anchor_type == NODE_PROCESS_BATCH:
            if anchor_id not in self._process_batches:
                raise NotFound("加工批次")
            self._add_downstream_process(nodes, {anchor_id})
            return nodes
        elif anchor_type == NODE_PRODUCT:
            good = self._goods.get(anchor_id)
            if good is None:
                raise NotFound("成品")
            add(anchor_id, NODE_PRODUCT, good.重量kg)
            return nodes

        # 来源侧：过磅批次（磅单）。已交货的部分要扣除已转入加工的数量
        delivery_ids: set[str] = set()
        delivery_of_ticket: dict[str, str] = {}
        for tid in ticket_ids:
            t = self._tickets[tid]
            batch = self._batches[t.批次编号]
            if batch.交货单编号:
                delivery_ids.add(batch.交货单编号)
                delivery_of_ticket[tid] = batch.交货单编号
            else:
                add(tid, NODE_TICKET, t.重量kg)
        for did in delivery_ids:
            delivery = self._deliveries[did]
            tickets = [self._tickets[tid] for tid in ticket_ids
                       if delivery_of_ticket.get(tid) == did]
            processed = min(self._delivery_into_processing(did), delivery.毛重kg)
            remainders = self._prorate(
                {t.编号: t.重量kg for t in tickets}, delivery.毛重kg, processed)
            for tid, remainder in remainders.items():
                add(tid, NODE_TICKET, remainder)

        # 交货 → 退货/损耗 → 加工批次 → 成品
        pb_ids: set[str] = set()
        source_keys: set[tuple[str, str]] = set()
        for did in delivery_ids:
            source_keys.add(("交货", did))
            for r in self._returns.values():
                if r.交货单编号 == did:
                    source_keys.add(("退货", r.编号))
            for ls in self._losses.values():
                if ls.交货单编号 == did:
                    source_keys.add(("损耗", ls.编号))
        for stype, sid in source_keys:
            for pb in self._process_batches.values():
                if any(line["来源类型"] == stype and line["来源单号"] == sid
                       for line in pb.投入明细):
                    pb_ids.add(pb.编号)
        self._add_downstream_process(nodes, pb_ids)
        return nodes

    def _add_downstream_process(self, nodes: dict[str, dict], pb_ids: set[str]):
        for pbid in pb_ids:
            pb = self._process_batches[pbid]
            made = sum(g.重量kg for g in self._goods.values()
                       if g.加工批次编号 == pbid)
            # 加工批节点承载在制余量；已成品部分挂到成品节点
            wip = pb.投入合计kg - made
            if wip > 0:
                nodes[pbid] = {"类型": NODE_PROCESS_BATCH, "重量kg": wip}
            for g in self._goods.values():
                if g.加工批次编号 == pbid:
                    nodes[g.编号] = {"类型": NODE_PRODUCT, "重量kg": g.重量kg}

    @staticmethod
    def _prorate(weights: dict[str, Decimal], gross: Decimal,
                 processed: Decimal) -> dict[str, Decimal]:
        """把已转入加工的数量按磅单重量比例从各磅单扣除，尾差并入第一张磅单。"""
        if processed <= 0 or gross <= 0:
            return dict(weights)
        result: dict[str, Decimal] = {}
        quant = Decimal("0.001")
        allocated = Decimal("0")
        keys = list(weights)
        for i, k in enumerate(keys):
            if i == len(keys) - 1:
                cut = processed - allocated
            else:
                cut = (processed * weights[k] / gross).quantize(
                    quant, rounding=ROUND_HALF_UP)
                allocated += cut
            remainder = weights[k] - cut
            if remainder > 0:
                result[k] = remainder
        return result

    def _delivery_into_processing(self, delivery_id: str) -> Decimal:
        total = Decimal("0")
        sources = {("交货", delivery_id)}
        for r in self._returns.values():
            if r.交货单编号 == delivery_id:
                sources.add(("退货", r.编号))
        for ls in self._losses.values():
            if ls.交货单编号 == delivery_id:
                sources.add(("损耗", ls.编号))
        for pb in self._process_batches.values():
            for line in pb.投入明细:
                if (line["来源类型"], line["来源单号"]) in sources:
                    total += line["重量kg"]
        return total

    def _fund_eligible_weights(self, recall: Recall,
                               settlement: Settlement) -> dict[str, Decimal]:
        """计算该结算单每张磅单与当前召回范围对应的受影响重量。

        直接受控的磅单取其任务受控量；范围内加工批/成品则按加工投入构成把数量
        沿父子边摊回来源交货，再按磅单重量比例摊到磅单。
        """
        eligible: dict[str, Decimal] = {}
        per_delivery: dict[str, Decimal] = {}

        def attribute(pb: ProcessingBatch, weight: Decimal):
            for line in pb.投入明细:
                st, sid = line["来源类型"], line["来源单号"]
                share = weight * line["重量kg"] / pb.投入合计kg
                if st == "交货":
                    did = sid
                elif st == "退货":
                    did = self._returns[sid].交货单编号
                else:
                    did = self._losses[sid].交货单编号
                per_delivery[did] = per_delivery.get(did, Decimal("0")) + share

        for task in self._recall_tasks.values():
            if task.召回编号 != recall.编号 or task.移出版本 is not None:
                continue
            if task.节点类型 == NODE_TICKET:
                ticket = self._tickets[task.节点编号]
                if ticket.结算编号 == settlement.编号:
                    eligible[task.节点编号] = \
                        eligible.get(task.节点编号, Decimal("0")) + task.受控数量kg
            elif task.节点类型 == NODE_PROCESS_BATCH:
                attribute(self._process_batches[task.节点编号], task.受控数量kg)
            elif task.节点类型 == NODE_PRODUCT:
                good = self._goods[task.节点编号]
                attribute(self._process_batches[good.加工批次编号], task.受控数量kg)

        did = settlement.交货单编号
        attributed = per_delivery.get(did, Decimal("0"))
        if attributed > 0:
            rows = {r["磅单编号"]: Decimal(r["重量kg"])
                    for r in settlement.行 if "磅单编号" in r}
            gross = sum(rows.values(), Decimal("0"))
            attributed = min(attributed, gross)
            remainders = self._prorate(rows, gross, attributed)
            for tid, line_weight in rows.items():
                cut = line_weight - remainders.get(tid, Decimal("0"))
                if cut > 0:
                    eligible[tid] = eligible.get(tid, Decimal("0")) + cut
        return eligible

    def rescope_recall(self, actor: Actor, recall_id: str, node_type: str,
                       node_id: str, note: str = "") -> dict:
        """以新锚点重算范围：扩大补建任务；缩小不自动释放已被人工隔离的货物。"""
        self._require_role(actor, {ROLE_REGULATOR}, "调整召回范围")
        with self._lock:
            recall = self._recalls.get(recall_id)
            if recall is None:
                raise NotFound("召回单")
            self._require_recall_open(recall)
            new_affected = self._affected_nodes(node_type, node_id)
            new_ids = set(new_affected)
            old_ids = set(recall.范围内节点)
            if new_ids == old_ids:
                raise Conflict("scope_unchanged", "新范围与当前版本一致，无需出新版")
            op = "扩大" if new_ids > old_ids and not (old_ids - new_ids) else \
                 "缩小" if old_ids > new_ids and not (new_ids - old_ids) else "调整"
            self._apply_scope(recall, new_affected, op, note,
                              removed=old_ids - new_ids)
            recall.发起节点类型 = node_type
            recall.发起节点编号 = node_id
            return self._recall_view(recall, actor)

    def _apply_scope(self, recall: Recall, affected: dict[str, dict], op: str,
                     note: str, removed: set[str] | None = None):
        """按范围差异补建/移出任务并落一个不可变的范围版本。"""
        version = recall.当前版本 + 1
        for node_id, info in affected.items():
            if node_id in recall.范围内节点:
                # 已在范围内：仅当任务尚未被通知/触碰时，按最新父子数量关系校正受控量；
                # 一旦对外通知或有处置，数量以既有任务为准（差异走冲突授权复核）。
                weight = info["重量kg"]
                for t in self._recall_tasks.values():
                    if t.召回编号 != recall.编号 or t.节点编号 != node_id \
                            or t.移出版本 is not None:
                        continue
                    untouched = t.状态 == TASK_PENDING and not t.人工隔离 \
                        and t.已处置kg == 0 and not any(
                            n.任务编号 == t.编号 for n in self._recall_notices.values())
                    if untouched and t.受控数量kg != weight:
                        t.受控数量kg = weight
                continue
            ntype, weight = info["类型"], info["重量kg"]
            custodian, custodian_role, default_action = self._custody_of(ntype, node_id)
            tid = self._id("召回任务")
            task = RecallTask(
                编号=tid, 召回编号=recall.编号, 节点类型=ntype, 节点编号=node_id,
                保管方=custodian, 保管方角色=custodian_role, 动作=default_action,
                受控数量kg=weight, 加入版本=version,
            )
            self._recall_tasks[tid] = task
        if removed:
            for node_id in removed:
                for task in self._recall_tasks.values():
                    if task.召回编号 != recall.编号 or task.节点编号 != node_id:
                        continue
                    if task.移出版本 is not None:
                        continue
                    task.移出版本 = version
                    # 已人工隔离/已实际处置的货物：缩范围不自动释放，留待人工决定
                    if task.人工隔离 or task.已处置kg > 0 or task.状态 == TASK_DISPUTED:
                        task.备注 = (task.备注 + " 范围缩小移出但保留人工处置").strip()
                    else:
                        # 未被人触碰的任务：系统直接移出，不产生对外业务效果
                        task.状态 = TASK_DONE
                        task.关闭版本 = version
                        task.处置去向["解除（系统移出）"] = task.受控数量kg
        recall.范围内节点 = set(affected)
        recall.当前版本 = version
        recall.版本历史.append(RecallScopeVersion(
            版本=version, 时间=_now(), 操作=op, 节点=sorted(affected), 说明=note))

    def _custody_of(self, ntype: str, node_id: str) -> tuple[str, str, str]:
        """定位节点的当前保管方与首选处置动作。"""
        if ntype == NODE_TICKET:
            ticket = self._tickets[node_id]
            batch = self._batches[ticket.批次编号]
            if batch.状态 == "已交货":
                return self._deliveries[batch.交货单编号].企业编号, ROLE_ENTERPRISE, ACTION_INTERCEPT
            return batch.农户编号, ROLE_FARMER, ACTION_QUARANTINE
        if ntype == NODE_PROCESS_BATCH:
            return self._process_batches[node_id].加工方, ROLE_PROCESSOR, ACTION_QUARANTINE
        good = self._goods[node_id]
        action = ACTION_INTERCEPT if good.状态 == "在途" else ACTION_QUARANTINE
        return good.当前保管方, good.保管方角色, action

    # ------------------------------------------------------------------ 通知与离线回执

    def notify_task(self, actor: Actor, recall_id: str, task_id: str,
                    channel: str, content: str) -> dict:
        """向保管方推送处置通知。重复通知（同任务同渠道同内容）只产生一次业务效果。"""
        self._require_role(actor, {ROLE_REGULATOR, ROLE_COOP}, "发送召回通知")
        with self._lock:
            recall, task = self._load_recall_task(recall_id, task_id)
            self._require_recall_open(recall)
            fingerprint = f"{channel}|{content}"
            for n in self._recall_notices.values():
                if n.召回编号 == recall_id and n.任务编号 == task_id \
                        and n.内容指纹 == fingerprint:
                    return {"通知": self._notice_view(n), "重复": True}
            nid = self._id("召回通知")
            notice = RecallNotice(nid, recall_id, task_id, channel, _now(), fingerprint)
            self._recall_notices[nid] = notice
            if task.状态 == TASK_PENDING:
                task.状态 = TASK_NOTIFIED
            return {"通知": self._notice_view(notice), "重复": False}

    def submit_receipt(self, actor: Actor, recall_id: str, task_id: str,
                       receipt_no: str, action: str, weight_kg,
                       offline=False, manual=False, note: str = "") -> dict:
        """保管方离线/在线回执。回执号幂等：同号重传只返回原回执，不重复生效。

        回执数量与系统受控数量不符时不做处置，登记冲突，等待监管授权复核。
        """
        with self._lock:
            recall, task = self._load_recall_task(recall_id, task_id)
            self._require_recall_open(recall)
            self._require_custodian(actor, task)
            if action not in DISPOSE_ACTIONS:
                raise ValidationFailed(
                    "回执动作必须是 隔离/拦截/退回/销毁/解除/替代交付")
            qty = self._dec(weight_kg, "回执数量")
            if qty <= 0:
                raise ValidationFailed("回执数量必须大于零")

            for r in self._recall_receipts.values():
                if r.召回编号 == recall_id and r.回执号 == receipt_no:
                    return {"回执": self._receipt_view(r), "冲突": None, "重复": True}

            notice_id = next((n.编号 for n in self._recall_notices.values()
                              if n.任务编号 == task_id), None)
            rid = self._id("召回回执")
            receipt = RecallReceipt(
                编号=rid, 召回编号=recall_id, 任务编号=task_id, 回执号=receipt_no,
                保管方=task.保管方, 动作=action, 数量kg=qty, 时间=_now(),
                离线=bool(offline), 首次通知编号=notice_id)
            self._recall_receipts[rid] = receipt

            conflict = self._receipt_conflict(task, action, qty)
            if conflict is not None:
                cid = self._id("召回冲突")
                rec = RecallConflict(cid, recall_id, task_id, task.保管方,
                                     qty, conflict["系统数量"], conflict["原因"], _now())
                self._recall_conflicts[cid] = rec
                task.状态 = TASK_DISPUTED
                return {"回执": self._receipt_view(receipt),
                        "冲突": self._conflict_view(rec), "重复": False}

            self._apply_receipt_effect(task, action, qty, manual)
            task.保管方已回执 = True
            return {"回执": self._receipt_view(receipt), "冲突": None, "重复": False}

    def _receipt_conflict(self, task: RecallTask, action: str,
                          qty: Decimal) -> dict | None:
        controlled = task.受控数量kg
        consumed = task.已处置kg
        released = task.处置去向.get(ACTION_RELEASE, Decimal("0"))
        held = task.处置去向.get(ACTION_QUARANTINE, Decimal("0")) + \
            task.处置去向.get(ACTION_INTERCEPT, Decimal("0"))
        if action in CONSUMING_ACTIONS:
            if consumed + qty > controlled:
                return {"系统数量": controlled - consumed,
                        "原因": f"申报{action} {qty}kg，超出可处置余额 "
                               f"{controlled - consumed}kg"}
        elif action in {ACTION_QUARANTINE, ACTION_INTERCEPT}:
            # 已销毁/退回/替代交付的部分不可能再被隔离
            if held + qty - consumed > controlled:
                return {"系统数量": controlled - held + consumed,
                        "原因": f"申报控制 {held + qty - consumed}kg 超过受控量 {controlled}kg"}
        elif action == ACTION_RELEASE:
            residual = controlled - consumed - released
            if qty > residual:
                return {"系统数量": residual,
                        "原因": f"申报解除 {qty}kg 超过未处置余额 {residual}kg"}
        return None

    def _apply_receipt_effect(self, task: RecallTask, action: str,
                              qty: Decimal, manual: bool):
        if action == ACTION_QUARANTINE:
            task.处置去向[ACTION_QUARANTINE] = \
                task.处置去向.get(ACTION_QUARANTINE, Decimal("0")) + qty
            if manual:
                task.人工隔离 = True
        elif action == ACTION_INTERCEPT:
            task.处置去向[ACTION_INTERCEPT] = \
                task.处置去向.get(ACTION_INTERCEPT, Decimal("0")) + qty
        elif action in CONSUMING_ACTIONS:
            task.已处置kg += qty
            task.处置去向[action] = task.处置去向.get(action, Decimal("0")) + qty
            if action == ACTION_REPLACE and task.节点类型 == NODE_PRODUCT:
                good = self._goods[task.节点编号]
                good.状态 = "已替代"
        elif action == ACTION_RELEASE:
            task.处置去向[ACTION_RELEASE] = \
                task.处置去向.get(ACTION_RELEASE, Decimal("0")) + qty
        resolved = task.已处置kg + task.处置去向.get(ACTION_RELEASE, Decimal("0"))
        if resolved >= task.受控数量kg:
            task.状态 = TASK_DONE

    def resolve_conflict(self, actor: Actor, conflict_id: str, decision: str,
                         accepted_kg=None, opinion: str = "") -> dict:
        """监管人员对数量冲突授权复核：维持原系统数量（驳回回执）或按认定数量调整。"""
        self._require_role(actor, {ROLE_REGULATOR}, "授权复核召回冲突")
        with self._lock:
            conf = self._recall_conflicts.get(conflict_id)
            if conf is None:
                raise NotFound("召回冲突")
            recall = self._recalls[conf.召回编号]
            self._require_recall_open(recall)
            if conf.状态 != "待授权":
                raise Conflict("conflict_resolved", "该冲突已授权复核")
            task = self._recall_tasks[conf.任务编号]
            if decision == "维持":
                conf.状态 = "已驳回"
                notified = any(n.任务编号 == task.编号 for n in self._recall_notices.values())
                task.状态 = TASK_NOTIFIED if notified else TASK_PENDING
            elif decision == "调整":
                if accepted_kg is None:
                    raise ValidationFailed("授权调整必须给出认定数量kg")
                accepted = self._dec(accepted_kg, "认定数量")
                receipt = self._latest_receipt(conf.召回编号, conf.任务编号)
                self._apply_receipt_effect(
                    task, receipt.动作, accepted,
                    manual=receipt.动作 == ACTION_QUARANTINE)
                task.保管方已回执 = True
                conf.状态 = "已授权调整"
                conf.认定数量kg = accepted
                # 隔离/拦截不是终态：若数量尚未交代完，回到待回执等待后续处置
                if task.状态 == TASK_DISPUTED:
                    task.状态 = TASK_NOTIFIED
            else:
                raise ValidationFailed("复核决定必须是 维持 / 调整")
            conf.授权人 = actor.name
            conf.授权时间 = _now()
            conf.授权意见 = opinion
            return self._conflict_view(conf)

    def release_quarantine(self, actor: Actor, recall_id: str, task_id: str,
                           weight_kg=None, note: str = "") -> dict:
        """监管/合作社对人工隔离货物的显式解除——缩范围不会自动走到这里。"""
        self._require_role(actor, {ROLE_REGULATOR, ROLE_COOP}, "解除人工隔离")
        with self._lock:
            recall, task = self._load_recall_task(recall_id, task_id)
            self._require_recall_open(recall)
            residual = task.受控数量kg - task.已处置kg \
                - task.处置去向.get(ACTION_RELEASE, Decimal("0"))
            qty = residual if weight_kg is None else self._dec(weight_kg, "解除数量")
            if qty <= 0 or qty > residual:
                raise ValidationFailed(f"可解除余额为 {residual}kg")
            task.授权解除 = True
            self._apply_receipt_effect(task, ACTION_RELEASE, qty, False)
            task.备注 = (task.备注 + f" 授权解除：{note}").strip()
            return self._task_view(task)

    # ------------------------------------------------------------------ 资金追加账

    def hold_funds(self, actor: Actor, recall_id: str, settlement_id: str,
                   weight_kg=None, basis: str = "") -> dict:
        """确认受影响后暂缓支付：以追加账记录，历史结算行一个字都不改。"""
        return self._fund_action(actor, recall_id, settlement_id, FUND_HOLD,
                                 weight_kg, basis)

    def claim_back_funds(self, actor: Actor, recall_id: str, settlement_id: str,
                         weight_kg=None, basis: str = "") -> dict:
        """对已付款部分追回：同样只追加，不改原结算。"""
        return self._fund_action(actor, recall_id, settlement_id, FUND_CLAIM_BACK,
                                 weight_kg, basis)

    def _fund_action(self, actor: Actor, recall_id: str, settlement_id: str,
                     kind: str, weight_kg, basis: str) -> dict:
        self._require_role(actor, {ROLE_REGULATOR, ROLE_COOP}, f"登记召回资金{kind}")
        with self._lock:
            recall = self._recalls.get(recall_id)
            if recall is None:
                raise NotFound("召回单")
            self._require_recall_open(recall)
            settlement = self._settlements.get(settlement_id)
            if settlement is None:
                raise NotFound("结算单")
            # 只可对召回范围内（含沿父子关系反查到的来源过磅批次）的款项暂缓/追回，
            # 金额按结算固化单价计算，与等级申诉价差调整各走各的追加账。
            eligible = self._fund_eligible_weights(recall, settlement)
            if not eligible:
                raise ValidationFailed("该结算单没有处于召回范围内的磅单")
            wanted = None if weight_kg is None else self._dec(weight_kg, f"{kind}重量")
            occupancy = self._fund_holds.setdefault(settlement_id, {})
            entries = []
            remaining = wanted
            total_amount = Decimal("0")
            for row in settlement.行:
                if "磅单编号" not in row:
                    continue
                if wanted is not None and remaining == 0:
                    break
                cap = eligible.get(row["磅单编号"], Decimal("0"))
                used = occupancy.get(row["磅单编号"], Decimal("0"))
                avail = min(Decimal(row["重量kg"]), cap) - used
                if avail <= 0:
                    continue
                take = avail if remaining is None else min(avail, remaining)
                unit = Decimal(row["单价"])
                amount = unit * take
                total_amount += amount
                eid = self._id("召回资金")
                entry = RecallFundEntry(
                    编号=eid, 召回编号=recall_id, 类型=kind, 结算编号=settlement_id,
                    农户编号=settlement.农户编号, 磅单编号=row["磅单编号"],
                    重量kg=take, 金额=_money(amount),
                    时间=_now(), 依据=basis or recall.原因,
                    操作人=actor.name)
                self._recall_funds[eid] = entry
                occupancy[row["磅单编号"]] = used + take
                entries.append(entry)
                if remaining is not None:
                    remaining -= take
            if not entries:
                raise Conflict("funds_already_covered",
                               "范围内磅单的款项均已暂缓/追回，无可用余额")
            if wanted is not None and remaining > 0:
                raise Conflict(
                    "funds_weight_exceeded",
                    f"范围内可{kind}重量不足，尚差 {remaining}kg（已登记部分请核对）")
            return {"类型": kind, "笔数": len(entries),
                    "合计重量kg": str(sum((e.重量kg for e in entries), Decimal("0"))),
                    "合计金额": _money(total_amount),
                    "明细": [self._fund_view(e) for e in entries]}

    def restore_funds(self, actor: Actor, recall_id: str, fund_entry_id: str) -> dict:
        """解除暂缓/追回：以恢复追加账对冲原记录，原记录保留不删。"""
        self._require_role(actor, {ROLE_REGULATOR, ROLE_COOP}, "恢复召回资金")
        with self._lock:
            recall = self._recalls.get(recall_id)
            if recall is None:
                raise NotFound("召回单")
            self._require_recall_open(recall)
            original = self._recall_funds.get(fund_entry_id)
            if original is None or original.召回编号 != recall_id:
                raise NotFound("原资金记录")
            if original.类型 not in {FUND_HOLD, FUND_CLAIM_BACK}:
                raise ValidationFailed("只能对 暂缓/追回 记录做恢复")
            if any(e.关联编号 == fund_entry_id for e in self._recall_funds.values()):
                raise Conflict("already_restored", "该笔资金已恢复，不能重复恢复")
            eid = self._id("召回资金")
            entry = RecallFundEntry(
                编号=eid, 召回编号=recall_id, 类型=FUND_RESTORE,
                结算编号=original.结算编号, 农户编号=original.农户编号,
                磅单编号=original.磅单编号,
                重量kg=original.重量kg, 金额=original.金额, 时间=_now(),
                依据=f"恢复：{original.编号}", 关联编号=fund_entry_id,
                操作人=actor.name)
            self._recall_funds[eid] = entry
            # 释放占用，允许后续重新暂缓
            occupancy = self._fund_holds.get(original.结算编号, {})
            left = occupancy.get(original.磅单编号, Decimal("0")) - original.重量kg
            if left <= 0:
                occupancy.pop(original.磅单编号, None)
            else:
                occupancy[original.磅单编号] = left
            return self._fund_view(entry)

    def confirm_result(self, actor: Actor, recall_id: str, affected: bool,
                       opinion: str = "") -> dict:
        """复核结论：确认受影响 / 未受影响。未受影响时自动恢复全部未对冲的暂缓与追回。"""
        self._require_role(actor, {ROLE_REGULATOR}, "出具召回复核结论")
        with self._lock:
            recall = self._recalls.get(recall_id)
            if recall is None:
                raise NotFound("召回单")
            self._require_recall_open(recall)
            restored = []
            if not affected:
                recall.确认未受影响 = True
                restored_ids = {e.关联编号 for e in self._recall_funds.values()}
                for entry in list(self._recall_funds.values()):
                    if entry.召回编号 == recall_id \
                            and entry.类型 in {FUND_HOLD, FUND_CLAIM_BACK} \
                            and entry.编号 not in restored_ids:
                        rid = self._id("召回资金")
                        rev = RecallFundEntry(
                            编号=rid, 召回编号=recall_id, 类型=FUND_RESTORE,
                            结算编号=entry.结算编号, 农户编号=entry.农户编号,
                            磅单编号=entry.磅单编号,
                            重量kg=entry.重量kg, 金额=entry.金额, 时间=_now(),
                            依据=f"复核未受影响，恢复：{entry.编号}",
                            关联编号=entry.编号, 操作人=actor.name)
                        self._recall_funds[rid] = rev
                        restored.append(rid)
                for entry in self._recall_funds.values():
                    if entry.召回编号 == recall_id and entry.关联编号:
                        occupancy = self._fund_holds.get(entry.结算编号, {})
                        left = occupancy.get(entry.磅单编号, Decimal("0")) - entry.重量kg
                        if left <= 0:
                            occupancy.pop(entry.磅单编号, None)
                        else:
                            occupancy[entry.磅单编号] = left
            return {"召回编号": recall_id, "确认未受影响": not affected,
                    "意见": opinion, "自动恢复笔数": len(restored),
                    "恢复记录": restored}

    # ------------------------------------------------------------------ 结案

    def close_recall(self, actor: Actor, recall_id: str) -> dict:
        """结案：同时交代数量守恒、未回执节点与每笔资金变化。"""
        self._require_role(actor, {ROLE_REGULATOR}, "召回结案")
        with self._lock:
            recall = self._recalls.get(recall_id)
            if recall is None:
                raise NotFound("召回单")
            self._require_recall_open(recall)

            tasks = [t for t in self._recall_tasks.values()
                     if t.召回编号 == recall_id]
            active = [t for t in tasks if t.移出版本 is None]
            pending_conflicts = [c.编号 for c in self._recall_conflicts.values()
                                 if c.召回编号 == recall_id and c.状态 == "待授权"]
            if pending_conflicts:
                raise Conflict("conflicts_open",
                               f"存在待授权复核的冲突：{pending_conflicts}")
            # 人工隔离的货物缩范围不自动释放，结案前必须显式销毁/退回/替代/解除
            manual_held = [
                t.编号 for t in tasks
                if t.人工隔离
                and t.已处置kg + t.处置去向.get(ACTION_RELEASE, Decimal("0"))
                < t.受控数量kg
            ]
            if manual_held:
                raise Conflict(
                    "manual_quarantine_open",
                    f"仍有被人工隔离的货物未显式处置（缩小范围不会自动释放）：{manual_held}")

            # 数量守恒：受控 = 销毁+退回+替代交付+解除（含系统移出解除）
            balance = []
            totals = {ACTION_DESTROY: Decimal("0"), ACTION_RETURN: Decimal("0"),
                      ACTION_REPLACE: Decimal("0"), ACTION_RELEASE: Decimal("0"),
                      "受控": Decimal("0")}
            for t in tasks:
                cols = {a: t.处置去向.get(a, Decimal("0")) for a in
                        (ACTION_DESTROY, ACTION_RETURN, ACTION_REPLACE)}
                released = t.处置去向.get(ACTION_RELEASE, Decimal("0")) + \
                    t.处置去向.get("解除（系统移出）", Decimal("0"))
                accounted = sum(cols.values(), Decimal("0")) + released
                if t.移出版本 is None and accounted != t.受控数量kg:
                    raise Conflict(
                        "quantity_not_conserved",
                        f"任务 {t.编号} 数量不守恒：受控 {t.受控数量kg}kg，"
                        f"已交代 {accounted}kg")
                row = {"任务编号": t.编号, "节点类型": t.节点类型,
                       "节点编号": t.节点编号, "保管方": t.保管方,
                       "受控kg": str(t.受控数量kg),
                       ACTION_DESTROY + "kg": str(cols[ACTION_DESTROY]),
                       ACTION_RETURN + "kg": str(cols[ACTION_RETURN]),
                       ACTION_REPLACE + "kg": str(cols[ACTION_REPLACE]),
                       ACTION_RELEASE + "kg": str(released),
                       "在范围内": t.移出版本 is None}
                balance.append(row)
                if t.移出版本 is None:
                    totals["受控"] += t.受控数量kg
                    totals[ACTION_DESTROY] += cols[ACTION_DESTROY]
                    totals[ACTION_RETURN] += cols[ACTION_RETURN]
                    totals[ACTION_REPLACE] += cols[ACTION_REPLACE]
                    totals[ACTION_RELEASE] += released

            unresolved = [t.编号 for t in active if t.状态 != TASK_DONE]
            if unresolved:
                raise Conflict(
                    "tasks_open",
                    f"仍有任务未完成处置：{unresolved}，不能结案")

            no_receipt = [
                {"任务编号": t.编号, "节点类型": t.节点类型,
                 "节点编号": t.节点编号, "保管方": t.保管方,
                 "原因": "系统移出，未获保管方回执" if t.移出版本 is not None
                         else "无回执即关闭"}
                for t in tasks
                if not t.保管方已回执
            ]

            funds = [self._fund_view(e) for e in self._recall_funds.values()
                     if e.召回编号 == recall_id]
            hold_sum = sum((Decimal(e["金额"]) for e in funds
                            if e["类型"] == FUND_HOLD), Decimal("0"))
            back_sum = sum((Decimal(e["金额"]) for e in funds
                            if e["类型"] == FUND_CLAIM_BACK), Decimal("0"))
            restore_sum = sum((Decimal(e["金额"]) for e in funds
                               if e["类型"] == FUND_RESTORE), Decimal("0"))

            report = {
                "召回编号": recall_id,
                "范围版本": recall.当前版本,
                "数量守恒": {
                    "受控合计kg": str(totals["受控"]),
                    "销毁kg": str(totals[ACTION_DESTROY]),
                    "退回kg": str(totals[ACTION_RETURN]),
                    "替代交付kg": str(totals[ACTION_REPLACE]),
                    "解除kg": str(totals[ACTION_RELEASE]),
                    "差额kg": str(totals["受控"] - totals[ACTION_DESTROY]
                                  - totals[ACTION_RETURN] - totals[ACTION_REPLACE]
                                  - totals[ACTION_RELEASE]),
                    "逐任务": balance,
                },
                "未回执节点": no_receipt,
                "资金变化": {
                    "暂缓": _money(hold_sum), "追回": _money(back_sum),
                    "恢复": _money(restore_sum),
                    "净影响": _money(-(hold_sum + back_sum - restore_sum)),
                    "逐笔": funds,
                },
            }
            recall.状态 = "已结案"
            recall.结案时间 = _now()
            recall.结案报告 = report
            return report

    # ------------------------------------------------------------------ 召回查询

    def list_recalls(self, actor: Actor) -> list[dict]:
        with self._lock:
            if actor.role == ROLE_GUARD:
                raise PermissionDenied("查看召回信息")
            out = []
            for recall in self._recalls.values():
                if actor.role in {ROLE_REGULATOR, ROLE_COOP}:
                    out.append(self._recall_view(recall, actor))
                elif self._tasks_visible(actor, recall):
                    out.append(self._recall_view(recall, actor))
            return out

    def get_recall(self, actor: Actor, recall_id: str) -> dict:
        with self._lock:
            recall = self._recalls.get(recall_id)
            if recall is None:
                raise NotFound("召回单")
            if actor.role == ROLE_GUARD:
                raise PermissionDenied("查看召回信息")
            if actor.role not in {ROLE_REGULATOR, ROLE_COOP} \
                    and not self._tasks_visible(actor, recall):
                raise PermissionDenied("查看非本保管方的召回单")
            return self._recall_view(recall, actor)

    def list_my_tasks(self, actor: Actor) -> list[dict]:
        """保管方履职视图：只看分派给自己的处置任务。"""
        with self._lock:
            if actor.role == ROLE_GUARD:
                raise PermissionDenied("查看召回任务")
            return [self._task_view(t) for t in self._recall_tasks.values()
                    if self._task_visible(actor, t)]

    def get_recall_task(self, actor: Actor, task_id: str) -> dict:
        with self._lock:
            task = self._recall_tasks.get(task_id)
            if task is None:
                raise NotFound("召回任务")
            if actor.role == ROLE_GUARD:
                raise PermissionDenied("查看召回任务")
            if actor.role not in {ROLE_REGULATOR, ROLE_COOP} \
                    and not self._task_visible(actor, task):
                raise PermissionDenied("查看他人保管任务")
            return self._task_view(task, include_receipts=True)

    def _tasks_visible(self, actor: Actor, recall: Recall) -> bool:
        return any(self._task_visible(actor, t) for t in self._recall_tasks.values()
                   if t.召回编号 == recall.编号)

    def _task_visible(self, actor: Actor, task: RecallTask) -> bool:
        if actor.role == ROLE_FARMER:
            return task.保管方角色 == ROLE_FARMER and actor.farmer_id == task.保管方
        if actor.role in {ROLE_ENTERPRISE, ROLE_PROCESSOR}:
            return actor.name == task.保管方
        return False

    # ------------------------------------------------------------------ 召回内部工具

    def _load_recall_task(self, recall_id: str, task_id: str):
        recall = self._recalls.get(recall_id)
        if recall is None:
            raise NotFound("召回单")
        task = self._recall_tasks.get(task_id)
        if task is None or task.召回编号 != recall_id:
            raise NotFound("召回任务")
        return recall, task

    @staticmethod
    def _require_recall_open(recall: Recall):
        if recall.状态 == "已结案":
            raise Conflict("recall_closed", "召回单已结案，不可再变更")

    def _require_custodian(self, actor: Actor, task: RecallTask):
        if actor.role in {ROLE_REGULATOR, ROLE_COOP}:
            return  # 监管/合作社可代录纸质离线回执
        if actor.role == ROLE_FARMER:
            if task.保管方角色 == ROLE_FARMER and actor.farmer_id == task.保管方:
                return
        elif actor.name == task.保管方 and actor.role == task.保管方角色:
            return
        raise PermissionDenied("代非本保管方提交回执")

    def _latest_receipt(self, recall_id: str, task_id: str) -> RecallReceipt:
        matches = [r for r in self._recall_receipts.values()
                   if r.召回编号 == recall_id and r.任务编号 == task_id]
        return matches[-1]

    def _ticket_under_active_recall(self, ticket_id: str) -> bool:
        return any(
            r.状态 != "已结案" and ticket_id in r.范围内节点
            for r in self._recalls.values()
        )

    # ------------------------------------------------------------------ 序列化视图

    def _plot_view(self, p: Plot) -> dict:
        return {"地块编号": p.编号, "农户编号": p.农户编号, "名称": p.名称, "地点": p.地点}

    def _tree_view(self, t: TreeGroup, actor: Actor | None = None) -> dict:
        view = {
            "编号": t.编号, "树群编号": t.编号, "地块编号": t.地块编号,
            "名称": t.名称, "树种": t.树种, "树龄年": t.树龄年,
            "保护级别": t.保护级别, "健康状态": t.健康状态,
            "本季已采重量kg": str(t.本季已采重量), "本季已采次数": t.本季已采次数,
        }
        if actor is not None and actor.role == ROLE_GUARD:
            return {k: view[k] for k in GUARD_TREE_VIEW if k in view}
        return view

    def _care_view(self, c: CareLog) -> dict:
        return {"编号": c.编号, "树群编号": c.树群编号, "日期": c.日期,
                "事项": c.事项, "记录人": c.记录人, "病害": c.病害,
                "病害描述": c.病害描述}

    def _batch_view(self, b: HarvestBatch) -> dict:
        return {"批次编号": b.编号, "地块编号": b.地块编号, "农户编号": b.农户编号,
                "采收日期": b.采收日期, "季": b.季, "状态": b.状态,
                "预占重量kg": str(b.预占重量), "来源树群": b.来源树群,
                "交货单编号": b.交货单编号}

    def _ticket_view(self, t: WeighTicket, actor: Actor | None = None, duplicated: bool = False) -> dict:
        view = {"磅单编号": t.编号, "批次编号": t.批次编号, "树群编号": t.树群编号,
                "过磅流水号": t.过磅流水号, "重量kg": str(t.重量kg),
                "毛重kg": str(t.毛重kg) if t.毛重kg is not None else None,
                "皮重kg": str(t.皮重kg) if t.皮重kg is not None else None,
                "过磅时间": t.过磅时间, "断网离线": t.断网离线, "设备号": t.设备号,
                "同步时间": t.同步时间, "检验编号": t.检验编号, "结算编号": t.结算编号}
        if duplicated:
            view["幂等命中"] = True
        return view

    def _inspection_view(self, i: Inspection) -> dict:
        return {"检验编号": i.编号, "磅单编号": i.磅单编号, "批次编号": i.批次编号,
                "检验时间": i.检验时间, "检验员": i.检验员, "等级": i.等级,
                "糖度": str(i.糖度) if i.糖度 is not None else None,
                "判定依据": i.判定依据, "样本编号": i.样本编号,
                "样本封存位置": i.样本封存位置, "来源": i.来源,
                "复核自": i.复核自, "现行": i.现行}

    def _rule_view(self, r: PriceRule) -> dict:
        return {"价规编号": r.编号, "版本": r.版本, "生效时间": r.生效时间,
                "保护价": {g: str(p) for g, p in r.保护价.items()},
                "质量系数": {g: str(c) for g, c in r.质量系数.items()},
                "市场价参考": {g: str(p) for g, p in r.市场价参考.items()},
                "备注": r.备注}

    def _contract_view(self, c: Contract) -> dict:
        return {"合约编号": c.编号, "农户编号": c.农户编号, "企业编号": c.企业编号,
                "签约时间": c.签约时间, "签约价规": c.价规编号,
                "约定等级": c.等级, "季": c.季}

    def _delivery_view(self, d: Delivery) -> dict:
        return {"交货单编号": d.编号, "企业编号": d.企业编号, "批次编号": d.批次编号,
                "农户编号": d.农户编号, "合约编号": d.合约编号,
                "交货时间": d.交货时间, "计价价规": d.价规编号,
                "核定毛重kg": str(d.毛重kg)}

    def _settlement_view(self, s: Settlement) -> dict:
        return {"结算编号": s.编号, "农户编号": s.农户编号, "批次编号": s.批次编号,
                "合约编号": s.合约编号, "交货单编号": s.交货单编号,
                "时间": s.时间, "状态": s.状态, "明细行": s.行,
                "价差调整": [self._adjustment_view(self._adjustments[a])
                            for a in s.调整编号]}

    def _adjustment_view(self, a: GradeAdjustment) -> dict:
        return {"价差编号": a.编号, "原检验编号": a.原检验编号,
                "新检验编号": a.新检验编号, "磅单编号": a.磅单编号,
                "原等级": a.原等级, "新等级": a.新等级,
                "原单价": a.原单价, "新单价": a.新单价,
                "重量kg": str(a.重量kg), "差额": a.差额, "方向": a.方向,
                "时间": a.时间, "结算编号": a.结算编号}

    def _return_view(self, r: EnterpriseReturn) -> dict:
        return {"退货编号": r.编号, "交货单编号": r.交货单编号,
                "批次编号": r.批次编号, "重量kg": str(r.重量kg),
                "原因": r.原因, "检验依据编号": r.检验依据编号,
                "处置": r.处置, "时间": r.时间}

    def _loss_view(self, r: TransitLoss) -> dict:
        return {"损耗编号": r.编号, "交货单编号": r.交货单编号,
                "批次编号": r.批次编号, "核定重量kg": str(r.核定重量kg),
                "到货重量kg": str(r.到货重量kg), "损耗kg": str(r.损耗kg),
                "核定人": r.核定人, "时间": r.时间, "备注": r.备注}

    def _route_view(self, r: ProcessingRoute) -> dict:
        return {"加工编号": r.编号, "来源类型": r.来源类型, "来源单号": r.来源单号,
                "批次编号": r.批次编号, "制品": r.制品,
                "投入重量kg": str(r.投入重量kg), "经办人": r.经办人, "时间": r.时间}

    def _violation_view(self, v: Violation) -> dict:
        return {"巡护编号": v.编号, "树群编号": v.树群编号, "地块编号": v.地块编号,
                "类型": v.类型, "描述": v.描述, "上报人": v.上报人,
                "时间": v.时间, "处理状态": v.处理状态}

    # ------------------------------------------------------------------ 加工/召回视图

    def _process_batch_view(self, p: ProcessingBatch) -> dict:
        return {"加工批次编号": p.编号, "加工方": p.加工方, "时间": p.时间,
                "制品": p.制品, "投入合计kg": str(p.投入合计kg),
                "投入明细": [
                    {"来源类型": ln["来源类型"], "来源单号": ln["来源单号"],
                     "重量kg": str(ln["重量kg"])} for ln in p.投入明细]}

    def _good_view(self, g: FinishedGood) -> dict:
        return {"成品编号": g.编号, "加工批次编号": g.加工批次编号, "名称": g.名称,
                "成品批次号": g.批次号, "重量kg": str(g.重量kg), "时间": g.时间,
                "当前保管方": g.当前保管方, "地点": g.地点, "状态": g.状态}

    def _task_view(self, t: RecallTask, include_receipts: bool = False) -> dict:
        view = {
            "任务编号": t.编号, "召回编号": t.召回编号,
            "节点类型": t.节点类型, "节点编号": t.节点编号,
            "保管方": t.保管方, "保管方角色": t.保管方角色,
            "处置动作": t.动作, "受控数量kg": str(t.受控数量kg),
            "状态": t.状态, "加入版本": t.加入版本, "移出版本": t.移出版本,
            "人工隔离": t.人工隔离, "已处置kg": str(t.已处置kg),
            "处置去向": {k: str(v) for k, v in t.处置去向.items()},
            "保管方已回执": t.保管方已回执, "备注": t.备注,
        }
        if include_receipts:
            view["回执"] = [
                self._receipt_view(r) for r in self._recall_receipts.values()
                if r.任务编号 == t.编号
            ]
        return view

    def _notice_view(self, n: RecallNotice) -> dict:
        return {"通知编号": n.编号, "召回编号": n.召回编号, "任务编号": n.任务编号,
                "渠道": n.渠道, "时间": n.时间}

    def _receipt_view(self, r: RecallReceipt) -> dict:
        return {"回执编号": r.编号, "召回编号": r.召回编号, "任务编号": r.任务编号,
                "回执号": r.回执号, "保管方": r.保管方, "动作": r.动作,
                "数量kg": str(r.数量kg), "时间": r.时间, "离线": r.离线,
                "首次通知编号": r.首次通知编号}

    def _conflict_view(self, c: RecallConflict) -> dict:
        return {"冲突编号": c.编号, "召回编号": c.召回编号, "任务编号": c.任务编号,
                "保管方": c.保管方, "上报数量kg": str(c.上报数量kg),
                "系统数量kg": str(c.系统数量kg), "原因": c.原因, "时间": c.时间,
                "状态": c.状态, "授权人": c.授权人, "授权时间": c.授权时间,
                "授权意见": c.授权意见,
                "认定数量kg": str(c.认定数量kg) if c.认定数量kg is not None else None}

    def _fund_view(self, e: RecallFundEntry) -> dict:
        return {"资金编号": e.编号, "召回编号": e.召回编号, "类型": e.类型,
                "结算编号": e.结算编号, "农户编号": e.农户编号,
                "磅单编号": e.磅单编号, "重量kg": str(e.重量kg), "金额": e.金额,
                "时间": e.时间, "依据": e.依据, "关联编号": e.关联编号,
                "操作人": e.操作人}

    def _version_view(self, v: RecallScopeVersion) -> dict:
        return {"版本": v.版本, "时间": v.时间, "操作": v.操作,
                "范围节点": v.节点, "说明": v.说明}

    def _recall_view(self, r: Recall, actor: Actor | None = None) -> dict:
        full = actor is not None and actor.role in {ROLE_REGULATOR, ROLE_COOP}
        tasks = [t for t in self._recall_tasks.values() if t.召回编号 == r.编号]
        view = {
            "召回编号": r.编号,
            "发起节点": {"类型": r.发起节点类型, "编号": r.发起节点编号},
            "原因": r.原因, "检验依据编号": r.检验依据编号,
            "发起人": r.发起人, "发起时间": r.发起时间,
            "当前版本": r.当前版本, "状态": r.状态,
            "版本历史": [self._version_view(v) for v in r.版本历史],
            "任务": [self._task_view(t) for t in tasks
                    if full or (actor is not None and self._task_visible(actor, t))],
        }
        if full:
            view["范围内节点"] = sorted(r.范围内节点)
            view["确认未受影响"] = r.确认未受影响
            view["未决冲突"] = [
                self._conflict_view(c) for c in self._recall_conflicts.values()
                if c.召回编号 == r.编号 and c.状态 == "待授权"]
            view["资金追加账"] = [
                self._fund_view(e) for e in self._recall_funds.values()
                if e.召回编号 == r.编号]
            if r.结案报告:
                view["结案报告"] = r.结案报告
        return view
