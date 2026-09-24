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
* **农残复核与多节点召回**：农残复检只追加记录、不改原检验与封存样本；召回可从
  地块 / 过磅批次 / 加工批次 / 成品任一节点发起，沿父子数量关系定位当前保管方
  （在库企业、在途承运、在制/成品加工方、已交付企业、退货持有），形成范围版本。
  扩大范围补建任务，缩小范围只自动释放系统隔离货、人工隔离货转为「出围待核」；
  通知与回执幂等（离线可补录），冲突等待监管授权复核；确认受影响后以追加账记
  暂缓/追回/恢复，原始结算不改写；结案强制数量守恒、回执齐全、资金可交代。
* **角色隔离**：护树队只能上报病害与违规采摘，读取树群管护视图，无法接触农户
  结算明细；不同农户之间也互不可见；召回数据按监管/合作社/企业/加工方/农户
  各自履职范围裁剪。
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

ROLE_FARMER = "果农"          # 查看本人地块/批次/应收
ROLE_COOP = "合作社"          # 全链管理与结算
ROLE_REVIEWER = "质量复核人"  # 取样、复核改级、农残复核
ROLE_GUARD = "护树队"         # 仅上报病害、违规采摘
ROLE_ENTERPRISE = "收购企业"  # 交货对接、成品原料反查、在库/在途货保管
ROLE_PROCESSOR = "加工方"     # 加工批次与成品的当前保管方
ROLE_REGULATOR = "监管人员"   # 发起召回、裁决冲突、守恒结案

# 结算明细属于合作社财务域，护树队无权查看
SETTLER_ROLES = {ROLE_COOP}
SETTLE_DETAIL_VIEWERS = {ROLE_COOP}

# 召回任务的当前保管方 -> 可代为回执的角色（承运在途货由托运货主企业代办）
CUSTODY_ROLES = {
    "合作社": {ROLE_COOP},
    "企业": {ROLE_ENTERPRISE, ROLE_COOP},
    "承运方": {ROLE_ENTERPRISE, ROLE_COOP},
    "加工方": {ROLE_PROCESSOR, ROLE_COOP},
}

# 护树队可见的最小树群视图
GUARD_TREE_VIEW = {"编号", "树群编号", "地块编号", "保护级别", "树种", "树龄年", "健康状态"}

# 召回处置动作与可发起节点
DISPOSE_ACTIONS = {"隔离", "拦截", "退回", "销毁", "解除", "替代交付"}
RECALL_ORIGIN_TYPES = {"地块", "过磅批次", "加工批次", "成品"}
FUND_KINDS = {"暂缓", "追回", "恢复"}


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
    在库kg: Decimal = Decimal("0")     # 仍在企业库的可处置量
    在途kg: Decimal = Decimal("0")     # 已发往加工方、承运中
    到厂kg: Decimal = Decimal("0")     # 已到加工方待投/在制
    加工方编号: str = ""               # 到厂确认时登记的当前加工方


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


@dataclass
class PesticideReview:
    """农残复核：与等级复核平行，原始检验与封存样本不可改写。"""
    编号: str
    来源检验编号: str             # 所复核的（现行）质量检验
    批次编号: str
    检验时间: str
    检验员: str
    项目: str                     # 如 克百威 / 多菌灵
    结果: str                     # 合格 / 不合格
    实测值: str
    限量值: str
    判定依据: str
    样本编号: str                 # 沿用原封存样本编号
    现行: bool = True
    复核自: str | None = None


@dataclass
class ProcessingBatch:
    """加工批次：原料来自若干交货批次，成品按投入重量分摊父子数量关系。"""
    编号: str
    企业编号: str
    加工方编号: str
    时间: str
    制品: str
    原料行: list[dict] = field(default_factory=list)   # 交货单/批次 + 投入重量
    成品: list[dict] = field(default_factory=list)     # 成品编号 + 重量


@dataclass
class FinishedGood:
    编号: str
    加工批次编号: str
    制品: str
    重量kg: Decimal
    时间: str
    保管方: str = "加工方"         # 加工方 / 企业 / 已交付客户
    物流状态: str = "在库"         # 在库 / 在途 / 已交付


@dataclass
class RecallVersion:
    """召回范围版本：扩围补建任务，缩围只标记出围，人工隔离的货不被自动释放。"""
    版本: int
    时间: str
    操作: str                     # 发起 / 扩大 / 缩小
    节点类型: str
    节点编号: str
    范围内: list[str] = field(default_factory=list)   # 本版本覆盖的溯源节点编号
    新增: list[str] = field(default_factory=list)
    移出: list[str] = field(default_factory=list)
    备注: str = ""


@dataclass
class RecallTask:
    """沿父子数量关系定位到当前保管方的处置单元（在库/在途/退货/在制/成品）。"""
    编号: str
    召回编号: str
    节点类型: str                 # 地块 / 过磅批次 / 交货单 / 退货单 / 加工批次 / 成品 / 加工路线
    节点编号: str
    保管方: str                   # 合作社 / 企业 / 承运方 / 加工方
    重量kg: Decimal
    范围键: str = ""
    位置: str = ""                # 在库 / 在途 / 到厂 / 退货 / 已交付
    农户编号: str | None = None
    状态: str = "待通知"           # 待通知/已通知/已隔离/已拦截/已退回/已销毁/已解除/已替代交付/出围待核
    隔离方式: str = ""             # 系统 / 人工
    在围: bool = True             # 是否处于当前召回范围版本
    隔离中kg: Decimal = Decimal("0")    # 当前在隔离/拦截余额（非累计）
    已销毁kg: Decimal = Decimal("0")
    已退回kg: Decimal = Decimal("0")
    已替代kg: Decimal = Decimal("0")
    已解除kg: Decimal = Decimal("0")
    替代重量kg: Decimal = Decimal("0")
    回执编号: str | None = None
    记录: list[dict] = field(default_factory=list)

    def 终态合计(self) -> Decimal:
        return self.已销毁kg + self.已退回kg + self.已替代kg + self.已解除kg

    def 未处置kg(self) -> Decimal:
        return self.重量kg - self.隔离中kg - self.终态合计()


@dataclass
class DisposalReceipt:
    """保管方处置回执：离线补录与重复通知都只产生一次业务效果。"""
    编号: str
    任务编号: str
    召回编号: str
    动作: str                     # 隔离/拦截/退回/销毁/解除/替代交付
    重量kg: Decimal
    时间: str
    回执人: str
    流水号: str = ""
    离线补录: bool = False
    备注: str = ""


@dataclass
class RecallNotice:
    """召回通知：同一任务重复通知幂等，离线重推只认首次。"""
    编号: str
    任务编号: str
    召回编号: str
    时间: str
    渠道: str
    离线: bool


@dataclass
class RecallConflict:
    """现场上报与系统范围冲突：挂起等待监管授权复核，不自动处置。"""
    编号: str
    任务编号: str
    召回编号: str
    上报内容: str
    上报人: str
    时间: str
    状态: str = "待授权复核"       # 待授权复核 / 维持隔离 / 解除隔离
    裁决人: str = ""
    裁决时间: str | None = None
    裁决意见: str = ""


@dataclass
class RecallFundEntry:
    """受影响确认后的追加账：暂缓/追回/恢复，原始结算与历史结算分文不改。"""
    编号: str
    召回编号: str
    结算编号: str
    农户编号: str
    种类: str                     # 暂缓 / 追回 / 恢复
    金额: str
    时间: str
    摘要: str
    关联追回编号: str | None = None


@dataclass
class Recall:
    """一次农残异常召回的聚合根：范围版本、任务、回执、冲突、追加账、结案报告。"""
    编号: str
    发起时间: str
    发起人: str
    起点类型: str
    起点编号: str
    异常依据编号: str             # 农残复核记录编号
    农户编号: str | None
    版本号: int = 0
    版本: list = field(default_factory=list)        # RecallVersion
    任务编号: list[str] = field(default_factory=list)
    回执编号: list[str] = field(default_factory=list)
    通知编号: list[str] = field(default_factory=list)
    冲突编号: list[str] = field(default_factory=list)
    追加账编号: list[str] = field(default_factory=list)
    状态: str = "进行中"           # 进行中 / 已结案
    结案时间: str | None = None
    结案报告: dict | None = None
    当前范围: set = field(default_factory=set)      # 节点编号集合（任务定位基准）


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

        # 农残复核与多节点召回
        self._pesticide: dict[str, PesticideReview] = {}
        self._proc_batches: dict[str, ProcessingBatch] = {}
        self._goods: dict[str, FinishedGood] = {}
        self._recalls: dict[str, Recall] = {}
        self._recall_tasks: dict[str, RecallTask] = {}
        self._receipts: dict[str, DisposalReceipt] = {}
        self._notices: dict[str, RecallNotice] = {}
        self._conflicts: dict[str, RecallConflict] = {}
        self._funds: dict[str, RecallFundEntry] = {}

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
                                contract_id, delivered_at, applicable.编号, gross,
                                在库kg=gross)
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
            # 退货果实转入独立退货持有单元，从正常在库量移出以保持召回数量守恒
            delivery.在库kg = max(Decimal("0"), delivery.在库kg - weight)
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
            # 损耗果实退出可处置持有，从在库量移出（退货已先行移出）
            delivery.在库kg = max(Decimal("0"), delivery.在库kg - loss)
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

    # ------------------------------------------------------------------ 农残复核

    def pesticide_review(self, actor: Actor, inspection_id: str, project: str,
                         result: str, measured: str, limit: str, basis: str) -> dict:
        """农残复核：安全项复检，不改变质量等级、不产生价差。

        原检验记录与封存样本不可改写；复测只追加新记录并标注「复核自」，
        不合格记录是监管发起召回的异常依据。
        """
        self._require_role(actor, {ROLE_REVIEWER, ROLE_REGULATOR}, "农残复核")
        if result not in {"合格", "不合格"}:
            raise ValidationFailed("农残结论必须是 合格 / 不合格")
        with self._lock:
            old = self._inspections.get(inspection_id)
            if old is None:
                raise NotFound("检验记录")
            if not old.现行:
                raise Conflict("superseded", "该检验已被后续复核取代，请对最新记录复核")
            # 同一检验若已有农残复核记录，旧记录留痕、新记录接续
            prev = next((p for p in self._pesticide.values()
                         if p.来源检验编号 == inspection_id and p.现行), None)
            if prev is not None:
                prev.现行 = False
            rid = self._id("农残")
            rec = PesticideReview(
                编号=rid, 来源检验编号=inspection_id, 批次编号=old.批次编号,
                检验时间=_now(), 检验员=actor.name, 项目=project, 结果=result,
                实测值=str(measured), 限量值=str(limit), 判定依据=basis,
                样本编号=old.样本编号, 复核自=prev.编号 if prev else None,
            )
            self._pesticide[rid] = rec
            return self._pesticide_view(rec)

    # ------------------------------------------------------------------ 加工链（在库/在途/成品）

    def ship_to_processing(self, actor: Actor, delivery_id: str, weight_kg) -> dict:
        """企业把到库原料发往加工方：在库 → 在途，沿父子关系移动数量。"""
        self._require_role(actor, {ROLE_COOP, ROLE_ENTERPRISE}, "发运加工原料")
        w = self._dec(weight_kg, "发运重量")
        with self._lock:
            d = self._deliveries.get(delivery_id)
            if d is None:
                raise NotFound("交货单")
            if w <= 0:
                raise ValidationFailed("发运重量必须大于零")
            if w > d.在库kg:
                raise ValidationFailed(
                    f"发运 {w}kg 超过该交货单当前在库 {d.在库kg}kg")
            d.在库kg -= w
            d.在途kg += w
            return self._delivery_view(d)

    def arrive_at_processor(self, actor: Actor, delivery_id: str, weight_kg) -> dict:
        """承运原料到达加工方：在途 → 到厂待投。"""
        self._require_role(actor, {ROLE_COOP, ROLE_ENTERPRISE, ROLE_PROCESSOR},
                           "确认原料到厂")
        w = self._dec(weight_kg, "到厂重量")
        with self._lock:
            d = self._deliveries.get(delivery_id)
            if d is None:
                raise NotFound("交货单")
            if w > d.在途kg:
                raise ValidationFailed(
                    f"到厂 {w}kg 超过在途 {d.在途kg}kg")
            d.在途kg -= w
            d.到厂kg += w
            if actor.role == ROLE_PROCESSOR:
                d.加工方编号 = actor.name
            return self._delivery_view(d)

    def create_processing_batch(self, actor: Actor, product: str, inputs: list[dict],
                                goods: list[dict], handler: str = "") -> dict:
        """加工方建档：投入来自若干交货单的到厂原料，按投入行形成父子数量关系，
        同批登记产出成品（成品重量可因脱水等不等于投入）。
        """
        self._require_role(actor, {ROLE_PROCESSOR, ROLE_COOP}, "建立加工批次")
        with self._lock:
            if not inputs:
                raise ValidationFailed("加工批次必须有原料投入行")
            if not goods:
                raise ValidationFailed("加工批次必须登记至少一项成品")
            rows = []
            enterprise = ""
            for row in inputs:
                did = row.get("交货单编号", "")
                d = self._deliveries.get(did)
                if d is None:
                    raise NotFound("原料交货单")
                amount = self._dec(row.get("投入重量kg"), "投入重量")
                used = sum(
                    Decimal(str(r["投入重量kg"]))
                    for pb in self._proc_batches.values() for r in pb.原料行
                    if r["交货单编号"] == did)
                queued = sum(Decimal(str(r["投入重量kg"])) for r in rows
                             if r["交货单编号"] == did)
                if used + queued + amount > d.到厂kg:
                    raise ValidationFailed(
                        f"交货单 {did} 到厂 {d.到厂kg}kg，已投/待投 "
                        f"{used + queued}kg，本次 {amount}kg 超出可投量")
                rows.append({"交货单编号": did, "批次编号": d.批次编号,
                             "投入重量kg": str(amount)})
                enterprise = enterprise or d.企业编号
            pid = self._id("加工批")
            pb = ProcessingBatch(pid, enterprise, actor.name, _now(), product, rows)
            for r in rows:
                self._deliveries[r["交货单编号"]].到厂kg -= Decimal(r["投入重量kg"])
            out = []
            for g in goods:
                gw = self._dec(g.get("重量kg"), "成品重量")
                if gw <= 0:
                    raise ValidationFailed("成品重量必须大于零")
                gid = self._id("成品")
                good = FinishedGood(gid, pid, g.get("制品", product), gw, _now(),
                                    保管方=g.get("保管方", "加工方"),
                                    物流状态=g.get("物流状态", "在库"))
                self._goods[gid] = good
                pb.成品.append({"成品编号": gid, "制品": good.制品,
                                "重量kg": str(gw)})
                out.append(self._goods_view(good))
            self._proc_batches[pid] = pb
            return {"加工批次": self._proc_batch_view(pb), "成品": out}

    def ship_finished_goods(self, actor: Actor, goods_id: str) -> dict:
        """成品发往客户：在库 → 在途，当前保管方仍为供货企业链条。"""
        self._require_role(actor, {ROLE_PROCESSOR, ROLE_COOP}, "发运成品")
        with self._lock:
            good = self._goods.get(goods_id)
            if good is None:
                raise NotFound("成品")
            if good.物流状态 != "在库":
                raise Conflict("goods_not_in_stock", "成品不在库，不能发运")
            good.物流状态 = "在途"
            return self._goods_view(good)

    def deliver_finished_goods(self, actor: Actor, goods_id: str) -> dict:
        """成品交付客户：在途 → 已交付，召回时由企业负责替代交付。"""
        self._require_role(actor, {ROLE_PROCESSOR, ROLE_COOP}, "成品交付确认")
        with self._lock:
            good = self._goods.get(goods_id)
            if good is None:
                raise NotFound("成品")
            if good.物流状态 != "在途":
                raise Conflict("goods_not_in_transit", "成品不在途，不能确认交付")
            good.物流状态 = "已交付"
            good.保管方 = "企业"
            return self._goods_view(good)

    # ------------------------------------------------------------------ 召回发起与范围版本

    def open_recall(self, actor: Actor, origin_type: str, origin_id: str,
                    pesticide_id: str, note: str = "") -> dict:
        """从地块 / 过磅批次 / 加工批次 / 成品任一节点发起召回。

        沿父子数量关系定位当前保管方，生成第一版范围与处置任务；
        不合格农残复核记录是唯一合法的发起依据。
        """
        self._require_role(actor, {ROLE_REGULATOR}, "发起召回")
        with self._lock:
            if origin_type not in RECALL_ORIGIN_TYPES:
                raise ValidationFailed("召回起点必须是 地块/过磅批次/加工批次/成品")
            pr = self._pesticide.get(pesticide_id)
            if pr is None:
                raise NotFound("农残复核记录")
            if pr.结果 != "不合格":
                raise Conflict("pesticide_qualified", "农残复核合格，不能据此发起召回")
            self._require_origin_exists(origin_type, origin_id)
            rid = self._id("召回")
            recall = Recall(rid, _now(), actor.name, origin_type, origin_id,
                            pesticide_id, self._farmer_of_origin(origin_type, origin_id))
            self._recalls[rid] = recall
            units = self._expand_units(origin_type, origin_id)
            if not units:
                raise Conflict("empty_recall_scope", "该节点当前没有可定位的货物，无需召回")
            self._commit_scope_version(recall, "发起", origin_type, origin_id,
                                       units, note)
            return self._recall_view(recall, actor)

    def reshape_recall(self, actor: Actor, recall_id: str, op: str,
                       origin_type: str, origin_id: str, note: str = "") -> dict:
        """范围改版：扩大取并集补建任务；缩小重算范围。

        缩小后：仅系统隔离、尚未人工处置的任务自动解除；已被人工隔离/拦截/
        退回/销毁的货物不自动释放，转为「出围待核」，须人工确认。
        """
        self._require_role(actor, {ROLE_REGULATOR}, "调整召回范围")
        if op not in {"扩大", "缩小"}:
            raise ValidationFailed("范围操作必须是 扩大 / 缩小")
        with self._lock:
            recall = self._recalls.get(recall_id)
            if recall is None:
                raise NotFound("召回单")
            if recall.状态 == "已结案":
                raise Conflict("recall_closed", "召回已结案，不能改围")
            if origin_type not in RECALL_ORIGIN_TYPES:
                raise ValidationFailed("召回起点必须是 地块/过磅批次/加工批次/成品")
            self._require_origin_exists(origin_type, origin_id)
            add_units = self._expand_units(origin_type, origin_id)
            if op == "扩大":
                have = {u["key"]: u for u in self._current_scope_units(recall)}
                for u in add_units:
                    have.setdefault(u["key"], u)
                units = list(have.values())
            else:
                units = add_units
            if not units:
                raise Conflict("empty_recall_scope", "调整后范围为空，不能提交；如需终止请结案")
            self._commit_scope_version(recall, op, origin_type, origin_id,
                                       units, note)
            return self._recall_view(recall, actor)

    def _require_origin_exists(self, origin_type: str, origin_id: str):
        if origin_type == "地块" and origin_id not in self._plots:
            raise NotFound("地块")
        if origin_type == "过磅批次" and origin_id not in self._batches:
            raise NotFound("过磅批次")
        if origin_type == "加工批次" and origin_id not in self._proc_batches:
            raise NotFound("加工批次")
        if origin_type == "成品" and origin_id not in self._goods:
            raise NotFound("成品")

    def _farmer_of_origin(self, origin_type: str, origin_id: str) -> str | None:
        if origin_type == "地块":
            return self._plots[origin_id].农户编号
        if origin_type == "过磅批次":
            return self._batches[origin_id].农户编号
        return None

    def _origin_deliveries(self, origin_type: str, origin_id: str) -> list[str]:
        """把任意起点归约为一组来源交货单。"""
        if origin_type == "地块":
            bids = [b.编号 for b in self._batches.values()
                    if b.地块编号 == origin_id]
        elif origin_type == "过磅批次":
            bids = [origin_id]
        elif origin_type == "加工批次":
            return [r["交货单编号"] for r in self._proc_batches[origin_id].原料行]
        else:  # 成品
            pb = self._proc_batches[self._goods[origin_id].加工批次编号]
            return [r["交货单编号"] for r in pb.原料行]
        out = []
        for b in self._batches.values():
            if b.编号 in bids and b.交货单编号:
                out.append(b.交货单编号)
        return out

    def _expand_units(self, origin_type: str, origin_id: str) -> list[dict]:
        """沿父子数量关系把起点展开为当前保管方处置单元。"""
        units: list[dict] = []
        delivery_ids = self._origin_deliveries(origin_type, origin_id)

        # 未交货的过磅批次：货仍在合作社
        if origin_type in {"地块", "过磅批次"}:
            bids = ([b for b in self._batches.values() if b.地块编号 == origin_id]
                    if origin_type == "地块" else [self._batches[origin_id]])
            for b in bids:
                if b.状态 == "已交货":
                    continue
                weight = sum(t.重量kg for t in self._tickets.values()
                             if t.批次编号 == b.编号)
                if weight > 0:
                    units.append(self._unit(f"批次:{b.编号}", "过磅批次", b.编号,
                                            "合作社", weight, "在库", b.农户编号))

        for did in delivery_ids:
            d = self._deliveries[did]
            if origin_type in {"加工批次", "成品"}:
                # 从成品/加工批次发起只追成品；同交货单尚未投入的余量由监管另行从
                # 过磅批次节点扩围，不擅自并入。
                continue
            if d.在库kg > 0:
                units.append(self._unit(f"交货:{did}:在库", "交货单", did,
                                        "企业", d.在库kg, "在库", d.农户编号))
            if d.在途kg > 0:
                units.append(self._unit(f"交货:{did}:在途", "交货单", did,
                                        "承运方", d.在途kg, "在途", d.农户编号))
            if d.到厂kg > 0:
                units.append(self._unit(f"交货:{did}:到厂", "交货单", did,
                                        "加工方", d.到厂kg, "到厂", d.农户编号))
            for r in self._returns.values():
                if r.交货单编号 != did:
                    continue
                processed = sum(rt.投入重量kg for rt in self._routes.values()
                                if rt.来源类型 == "退货" and rt.来源单号 == r.编号)
                left = r.重量kg - processed
                if left > 0:
                    units.append(self._unit(f"退货:{r.编号}", "退货单", r.编号,
                                            "企业", left, "退货", d.农户编号))

        # 成品：加工批次起点覆盖整批成品；成品起点仅覆盖该成品
        if origin_type == "加工批次":
            goods = [self._goods[g["成品编号"]]
                     for g in self._proc_batches[origin_id].成品]
        elif origin_type == "成品":
            goods = [self._goods[origin_id]]
        else:
            ids = {did for did in delivery_ids}
            goods = []
            for pb in self._proc_batches.values():
                if not any(r["交货单编号"] in ids for r in pb.原料行):
                    continue
                goods.extend(self._goods[g["成品编号"]] for g in pb.成品)
        for good in goods:
            # 交付前（在库/在途）由加工方负责拦截，交付客户后由供货企业负责替代
            keeper = {"在库": "加工方", "在途": "加工方", "已交付": "企业"}[good.物流状态]
            units.append(self._unit(f"成品:{good.编号}", "成品", good.编号,
                                    keeper, good.重量kg, good.物流状态, None))
        return units

    @staticmethod
    def _unit(key: str, node_type: str, node_id: str, keeper: str,
              weight: Decimal, location: str, farmer_id: str | None) -> dict:
        return {"key": key, "节点类型": node_type, "节点编号": node_id,
                "保管方": keeper, "重量kg": weight, "位置": location,
                "农户编号": farmer_id}

    def _all_scope_units(self, recall: Recall) -> list[dict]:
        """从当前任务还原范围单元（任务建后即持久，缩围不删除）。"""
        out = []
        for tid in recall.任务编号:
            t = self._recall_tasks[tid]
            out.append({"key": t.范围键, "节点类型": t.节点类型, "节点编号": t.节点编号,
                        "保管方": t.保管方, "重量kg": t.重量kg, "位置": t.位置,
                        "农户编号": t.农户编号})
        return out

    def _current_scope_units(self, recall: Recall) -> list[dict]:
        """仅当前版本仍在围的单元（扩围并集以此为基准，不含历史移出项）。"""
        return [u for u in self._all_scope_units(recall)
                if self._recall_tasks[next(
                    t for t in recall.任务编号
                    if self._recall_tasks[t].范围键 == u["key"])].在围]

    def _commit_scope_version(self, recall: Recall, op: str, origin_type: str,
                              origin_id: str, units: list[dict], note: str):
        tasks_by_key = {self._recall_tasks[i].范围键: self._recall_tasks[i]
                        for i in recall.任务编号}
        known = set(tasks_by_key)              # 历史上建过的全部任务
        before = {k for k, t in tasks_by_key.items() if t.在围}  # 上一版在围
        after = {u["key"] for u in units}
        new_keys = sorted(after - known)
        reenter_keys = sorted((after & known) - before)
        removed = sorted(before - after)

        for u in units:
            if u["key"] in known:
                task = tasks_by_key[u["key"]]
                if u["key"] in reenter_keys:
                    # 上一版被移出、本版重新纳入：
                    #  - 人工冻结的「出围待核」货保持冻结，不自动释放也不重置；
                    #  - 系统自动解除货清零上一轮桶后按当前位置重新隔离/待通知。
                    if task.状态 == "出围待核":
                        task.在围 = True
                    else:
                        task.在围 = True
                        task.隔离中kg = Decimal("0")
                        task.已销毁kg = task.已退回kg = Decimal("0")
                        task.已替代kg = task.已解除kg = Decimal("0")
                        task.隔离方式 = ""
                        if u["位置"] == "在库":
                            task.状态 = "已隔离"
                            task.隔离方式 = "系统"
                            task.隔离中kg = u["重量kg"]
                        else:
                            task.状态 = "待通知"
                        task.记录.append({"时间": _now(), "事件": "范围重新纳入",
                                          "重量kg": str(u["重量kg"])})
                task.重量kg = u["重量kg"]
                continue
            t = self._create_task(recall, u)
            tasks_by_key[t.范围键] = t
            # 在库货物由系统先行隔离（产生系统回执，不等人工）；在途/在制/已交付待保管方动作
            if t.位置 == "在库":
                t.状态 = "已隔离"
                t.隔离方式 = "系统"
                t.隔离中kg = t.重量kg
                sys_rid = self._id("回执")
                sys_rec = DisposalReceipt(sys_rid, t.编号, recall.编号, "隔离",
                                          t.重量kg, _now(), "系统",
                                          流水号=f"SYS-{t.编号}",
                                          备注="纳入范围时系统自动隔离")
                self._receipts[sys_rid] = sys_rec
                recall.回执编号.append(sys_rid)
                t.回执编号 = sys_rid
                t.记录.append({"时间": _now(), "事件": "隔离",
                              "重量kg": str(t.重量kg), "回执人": "系统"})
            recall.任务编号.append(t.编号)

        if op == "缩小" and removed:
            for key in removed:
                task = tasks_by_key[key]
                touched_manually = (
                    task.隔离方式 == "人工"
                    or task.状态 in {"已拦截", "已退回", "已销毁", "已替代交付"}
                )
                if touched_manually:
                    # 已被人工隔离/处置的货不自动释放：挂「出围待核」，数量仍冻结，
                    # 由保管方/监管在范围外另行人工确认解除或销毁。
                    task.状态 = "出围待核"
                    task.在围 = False
                    task.记录.append({"时间": _now(), "事件": "范围移出",
                                      "自动释放": False,
                                      "冻结kg": str(task.隔离中kg)})
                else:
                    released = task.隔离中kg
                    if released > 0:
                        task.隔离中kg = Decimal("0")
                        task.已解除kg += released
                    task.状态 = "已解除"
                    task.在围 = False
                    task.记录.append({"时间": _now(), "事件": "范围移出",
                                      "自动释放": True, "解除kg": str(released)})

        recall.当前范围 = after
        recall.版本号 += 1
        recall.版本.append(RecallVersion(
            版本=recall.版本号, 时间=_now(), 操作=op,
            节点类型=origin_type, 节点编号=origin_id,
            范围内=sorted(after), 新增=sorted(set(new_keys) | set(reenter_keys)),
            移出=removed, 备注=note))

    def _create_task(self, recall: Recall, u: dict) -> RecallTask:
        tid = self._id("召回任务")
        task = RecallTask(
            编号=tid, 召回编号=recall.编号, 节点类型=u["节点类型"],
            节点编号=u["节点编号"], 保管方=u["保管方"], 重量kg=u["重量kg"],
        )
        task.位置 = u["位置"]
        task.农户编号 = u["农户编号"]
        task.范围键 = u["key"]
        self._recall_tasks[tid] = task
        return task

    # ------------------------------------------------------------------ 通知与回执

    def notify_task(self, actor: Actor, task_id: str, channel: str = "系统消息",
                    offline: bool = False) -> dict:
        """向当前保管方发通知；同一任务重复通知只产生一次业务效果。"""
        with self._lock:
            task = self._load_task_for_keeper(actor, task_id)
            recall = self._recalls[task.召回编号]
            if recall.状态 == "已结案":
                raise Conflict("recall_closed", "召回已结案")
            existing = next((self._notices[n] for n in recall.通知编号
                             if self._notices[n].任务编号 == task_id), None)
            if existing is not None:
                return {"通知": self._notice_view(existing), "幂等命中": True}
            nid = self._id("通知")
            notice = RecallNotice(nid, task_id, recall.编号, _now(), channel, offline)
            self._notices[nid] = notice
            recall.通知编号.append(nid)
            if task.状态 == "待通知":
                task.状态 = "已通知"
            task.记录.append({"时间": _now(), "事件": "通知", "渠道": channel})
            return {"通知": self._notice_view(notice), "幂等命中": False}

    def disposal_receipt(self, actor: Actor, task_id: str, action: str,
                         receipt_no: str, weight_kg=None, offline=False,
                         note: str = "") -> dict:
        """保管方处置回执。

        离线补录与重复推送以 (任务, 回执流水号) 幂等，只产生一次业务效果；
        冲突未裁决前任务冻结；销毁必须先隔离/拦截。
        """
        if action not in DISPOSE_ACTIONS:
            raise ValidationFailed(f"处置动作必须是 {sorted(DISPOSE_ACTIONS)}")
        with self._lock:
            task = self._load_task_for_keeper(actor, task_id)
            recall = self._recalls[task.召回编号]
            if recall.状态 == "已结案":
                raise Conflict("recall_closed", "召回已结案，不能再回执")
            conflict = next((self._conflicts[c] for c in recall.冲突编号
                             if self._conflicts[c].任务编号 == task_id
                             and self._conflicts[c].状态 == "待授权复核"), None)
            if conflict is not None:
                raise Conflict("conflict_pending",
                               f"该任务存在待授权复核冲突（{conflict.编号}），裁决前冻结处置")
            dup = next((self._receipts[r] for r in recall.回执编号
                        if self._receipts[r].任务编号 == task_id
                        and getattr(self._receipts[r], "流水号", None) == receipt_no), None)
            if dup is not None:
                return {"回执": self._receipt_view(dup), "幂等命中": True}

            remaining = task.未处置kg()
            weight = task.重量kg if weight_kg is None else self._dec(weight_kg, "处置重量")
            if weight <= 0:
                raise ValidationFailed("处置重量必须大于零")

            if action == "拦截":
                if task.位置 != "在途":
                    raise Conflict("not_in_transit", "仅在途货物适用拦截")
                if weight > remaining:
                    raise ValidationFailed(f"拦截 {weight}kg 超过未处置量 {remaining}kg")
                task.隔离中kg += weight
                task.状态 = "已拦截"
            elif action == "隔离":
                if weight > remaining:
                    raise ValidationFailed(f"隔离 {weight}kg 超过未处置量 {remaining}kg")
                task.隔离中kg += weight
                task.状态 = "已隔离"
                # 现场人工隔离（含离线补录）一经确认，缩围不得自动释放
                if offline or task.隔离方式 != "系统":
                    task.隔离方式 = "人工"
            elif action == "销毁":
                if weight > task.隔离中kg:
                    raise Conflict(
                        "not_quarantined",
                        f"待销毁 {weight}kg 超过在隔离量 {task.隔离中kg}kg，先隔离/拦截")
                task.隔离中kg -= weight
                task.已销毁kg += weight
                task.状态 = "已销毁"
            elif action == "退回":
                available = remaining + task.隔离中kg
                if weight > available:
                    raise ValidationFailed(f"退回 {weight}kg 超过可处置量 {available}kg")
                from_quarantine = min(weight, task.隔离中kg)
                task.隔离中kg -= from_quarantine
                task.已退回kg += weight
                task.状态 = "已退回"
            elif action == "替代交付":
                if task.节点类型 != "成品":
                    raise Conflict("not_finished_goods", "仅成品可以替代交付")
                available = remaining + task.隔离中kg
                if weight > available:
                    raise ValidationFailed(f"替代 {weight}kg 超过可处置量 {available}kg")
                from_quarantine = min(weight, task.隔离中kg)
                task.隔离中kg -= from_quarantine
                task.已替代kg += weight
                task.替代重量kg = task.已替代kg
                task.状态 = "已替代交付"
            elif action == "解除":
                releasable = task.隔离中kg
                if weight_kg is None:
                    weight = releasable
                if weight > releasable:
                    raise ValidationFailed(f"解除 {weight}kg 超过在隔离量 {releasable}kg")
                task.隔离中kg -= weight
                task.已解除kg += weight
                task.状态 = "已解除"

            rid = self._id("回执")
            rec = DisposalReceipt(rid, task_id, recall.编号, action, weight, _now(),
                                  actor.name, bool(offline), note)
            rec.流水号 = receipt_no
            self._receipts[rid] = rec
            recall.回执编号.append(rid)
            task.回执编号 = rid
            task.记录.append({"时间": rec.时间, "事件": action, "重量kg": str(weight),
                              "离线补录": rec.离线补录, "回执人": actor.name})
            return {"回执": self._receipt_view(rec), "幂等命中": False}

    def report_conflict(self, actor: Actor, task_id: str, content: str) -> dict:
        """保管方现场发现数量/归属/结论冲突：挂起等待监管授权复核。"""
        with self._lock:
            task = self._load_task_for_keeper(actor, task_id)
            recall = self._recalls[task.召回编号]
            if recall.状态 == "已结案":
                raise Conflict("recall_closed", "召回已结案")
            pending = [c for c in recall.冲突编号
                       if self._conflicts[c].任务编号 == task_id
                       and self._conflicts[c].状态 == "待授权复核"]
            if pending:
                raise Conflict("conflict_pending",
                               f"该任务已有待授权复核冲突（{pending[0]}），勿重复上报")
            cid = self._id("冲突")
            conf = RecallConflict(cid, task_id, recall.编号, content, actor.name, _now())
            self._conflicts[cid] = conf
            recall.冲突编号.append(cid)
            task.记录.append({"时间": _now(), "事件": "上报冲突", "内容": content})
            return self._conflict_view(conf)

    def resolve_conflict(self, actor: Actor, conflict_id: str, decision: str,
                         opinion: str = "") -> dict:
        """监管授权复核裁决：维持隔离 / 解除隔离；解除联动恢复暂缓追回款。"""
        self._require_role(actor, {ROLE_REGULATOR}, "裁决召回冲突")
        if decision not in {"维持隔离", "解除隔离"}:
            raise ValidationFailed("裁决必须是 维持隔离 / 解除隔离")
        with self._lock:
            conf = self._conflicts.get(conflict_id)
            if conf is None:
                raise NotFound("召回冲突")
            if conf.状态 != "待授权复核":
                raise Conflict("conflict_resolved", "该冲突已经裁决")
            recall = self._recalls[conf.召回编号]
            task = self._recall_tasks[conf.任务编号]
            conf.状态 = decision
            conf.裁决人 = actor.name
            conf.裁决时间 = _now()
            conf.裁决意见 = opinion
            task.记录.append({"时间": _now(), "事件": f"裁决{decision}", "意见": opinion})
            if decision == "维持隔离":
                if task.隔离中kg == 0 and task.终态合计() == 0:
                    task.隔离中kg = task.重量kg
                task.状态 = "已隔离"
                task.隔离方式 = "人工"
            else:
                # 裁决确认不受影响：隔离余额与尚在途未处置余量一并解除
                released = task.隔离中kg + task.未处置kg()
                if released > 0:
                    task.隔离中kg = Decimal("0")
                    task.已解除kg += released
                task.状态 = "已解除"
                rid = self._id("回执")
                rec = DisposalReceipt(rid, task.编号, recall.编号, "解除", released,
                                      _now(), actor.name,
                                      流水号=f"SYS-CONF-{conf.编号}",
                                      备注=f"冲突 {conf.编号} 授权复核解除")
                self._receipts[rid] = rec
                recall.回执编号.append(rid)
                task.回执编号 = rid
                self._auto_restore_funds(actor, recall, task)
            return self._conflict_view(conf)

    # ------------------------------------------------------------------ 追加账（资金）

    def recall_fund(self, actor: Actor, recall_id: str, settlement_id: str,
                    kind: str, amount=None, summary: str = "",
                    linked_id: str | None = None) -> dict:
        """确认受影响后的追加账：暂缓 / 追回 / 恢复，原始结算行与历史结算不改写。

        与等级申诉的价差调整同构：各自独立追加，农户最终权益＝
        原结算 ＋ 等级价差 ＋ 召回追加账。
        """
        self._require_role(actor, {ROLE_REGULATOR, ROLE_COOP}, "登记召回追加账")
        if kind not in FUND_KINDS:
            raise ValidationFailed("追加账种类必须是 暂缓 / 追回 / 恢复")
        with self._lock:
            recall = self._recalls.get(recall_id)
            if recall is None:
                raise NotFound("召回单")
            settlement = self._settlements.get(settlement_id)
            if settlement is None:
                raise NotFound("结算单")
            base = self._settlement_base(settlement)
            entries = [self._funds[f] for f in recall.追加账编号
                       if self._funds[f].结算编号 == settlement_id]
            held = sum(Decimal(e.金额) for e in entries
                       if e.种类 in {"暂缓", "追回"})
            restored = sum(Decimal(e.金额) for e in entries if e.种类 == "恢复")
            open_held = held - restored
            if amount is None:
                if kind == "恢复":
                    value = open_held
                else:
                    value = base - open_held
            else:
                value = self._dec(amount, "金额")
            if value <= 0:
                raise ValidationFailed("追加账金额必须大于零")
            if kind in {"暂缓", "追回"} and open_held + value > base:
                raise Conflict(
                    "fund_exceeded",
                    f"{kind} {value} 元后累计挂账 {open_held + value} 元，"
                    f"超过该结算权益 {base} 元（含等级价差）")
            if kind == "恢复":
                if linked_id:
                    linked = next((e for e in entries if e.编号 == linked_id), None)
                    if linked is None or linked.种类 not in {"暂缓", "追回"}:
                        raise NotFound("被恢复的暂缓/追回记录")
                if value > open_held:
                    raise Conflict("restore_exceeded",
                                   f"恢复 {value} 元超过未解除挂账 {open_held} 元")
            fid = self._id("追加账")
            entry = RecallFundEntry(fid, recall_id, settlement_id,
                                    settlement.农户编号, kind, _money(value), _now(),
                                    summary, linked_id)
            self._funds[fid] = entry
            recall.追加账编号.append(fid)
            return self._fund_view(entry)

    def _auto_restore_funds(self, actor: Actor, recall: Recall, task: RecallTask):
        """冲突裁决解除时，把该农户在本召回下尚未恢复的挂账自动恢复。"""
        if not task.农户编号:
            return
        for sid in {self._funds[f].结算编号 for f in recall.追加账编号}:
            settlement = self._settlements.get(sid)
            if settlement is None or settlement.农户编号 != task.农户编号:
                continue
            entries = [self._funds[f] for f in recall.追加账编号
                       if self._funds[f].结算编号 == sid]
            held = sum(Decimal(e.金额) for e in entries if e.种类 in {"暂缓", "追回"})
            restored = sum(Decimal(e.金额) for e in entries if e.种类 == "恢复")
            if held - restored > 0:
                fid = self._id("追加账")
                value = held - restored
                entry = RecallFundEntry(
                    fid, recall.编号, sid, task.农户编号, "恢复", _money(value),
                    _now(), f"冲突 {self._latest_conflict(recall, task).编号} "
                            f"裁决解除，自动恢复挂账")
                self._funds[fid] = entry
                recall.追加账编号.append(fid)

    def _latest_conflict(self, recall: Recall, task: RecallTask) -> RecallConflict:
        return next(self._conflicts[c] for c in reversed(recall.冲突编号)
                    if self._conflicts[c].任务编号 == task.编号)

    def _settlement_base(self, settlement: Settlement) -> Decimal:
        """结算当前权益＝原合计 ＋ 全部等级价差（与等级申诉保持一致的基数）。"""
        total = next(Decimal(r["合计应收"]) for r in settlement.行 if "合计应收" in r)
        diffs = sum((Decimal(self._adjustments[a].差额)
                     for a in settlement.调整编号), Decimal("0"))
        return total + diffs

    # ------------------------------------------------------------------ 结案

    def recall_report(self, actor: Actor, recall_id: str) -> dict:
        # 守恒报告跨农户/企业/加工方汇总，仅监管与合作社可见全量
        self._require_role(actor, {ROLE_REGULATOR, ROLE_COOP}, "查看召回守恒结案报告")
        with self._lock:
            recall = self._recalls.get(recall_id)
            if recall is None:
                raise NotFound("召回单")
            return self._scoped_recall_view(recall, actor, report=True)

    def close_recall(self, actor: Actor, recall_id: str) -> dict:
        """监管守恒结案：数量必须勾稽、无未回执/未终态节点、无待裁决冲突，
        报告逐笔交代资金变化后冻结。
        """
        self._require_role(actor, {ROLE_REGULATOR}, "召回结案")
        with self._lock:
            recall = self._recalls.get(recall_id)
            if recall is None:
                raise NotFound("召回单")
            if recall.状态 == "已结案":
                raise Conflict("recall_closed", "召回已结案")
            report = self._build_report(recall)
            if not report["数量守恒"]:
                raise Conflict("quantity_not_balanced",
                               "数量未勾稽，不能结案")
            if report["待裁决冲突"]:
                raise Conflict("conflict_pending", "存在待授权复核冲突，不能结案")
            if report["未回执节点"]:
                raise Conflict("receipts_pending",
                               f"仍有 {len(report['未回执节点'])} 个节点未回执，不能结案")
            if report["未终态节点"]:
                raise Conflict("tasks_open",
                               f"仍有 {len(report['未终态节点'])} 个任务未终态，不能结案")
            if report["出围冻结节点"]:
                raise Conflict("manual_hold_open",
                               f"仍有 {len(report['出围冻结节点'])} 个人工隔离货缩围后"
                               "挂起未处置，须人工销毁或解除后才能结案")
            recall.状态 = "已结案"
            recall.结案时间 = _now()
            recall.结案报告 = report
            return self._recall_view(recall, actor)

    def _build_report(self, recall: Recall) -> dict:
        all_tasks = [self._recall_tasks[t] for t in recall.任务编号]
        tasks = [t for t in all_tasks if t.在围]
        out_held = [t for t in all_tasks if not t.在围 and t.状态 == "出围待核"]
        scope_weight = sum((t.重量kg for t in tasks), Decimal("0"))
        destroyed = sum((t.已销毁kg for t in tasks), Decimal("0"))
        returned = sum((t.已退回kg for t in tasks), Decimal("0"))
        replaced = sum((t.已替代kg for t in tasks), Decimal("0"))
        released = sum((t.已解除kg for t in tasks), Decimal("0"))
        in_quarantine = sum((t.隔离中kg for t in tasks), Decimal("0"))
        undisposed = sum((t.未处置kg() for t in tasks), Decimal("0"))
        decided = destroyed + returned + replaced + released
        # 守恒恒等式：在隔离 + 未处置 + 四类终态去向 必须恰好等于当前范围总重量。
        # 该式可在桶记账出现重复扣减时被打破，是结案的硬性勾稽；结案另要求未处置清零。
        balanced = in_quarantine + undisposed + decided == scope_weight
        no_receipt = [self._task_view(t) for t in tasks
                      if not any(self._receipts[r].任务编号 == t.编号
                                 for r in recall.回执编号)]
        open_tasks = [self._task_view(t) for t in tasks
                      if t.隔离中kg > 0 or t.未处置kg() > 0]
        funds = [self._funds[f] for f in recall.追加账编号]
        fund_by_settlement = {}
        for e in funds:
            row = fund_by_settlement.setdefault(e.结算编号, {
                "结算编号": e.结算编号, "农户编号": e.农户编号,
                "原结算权益": _money(self._settlement_base(self._settlements[e.结算编号])),
                "暂缓": "0.00", "追回": "0.00", "恢复": "0.00"})
            row[e.种类] = _money(Decimal(row[e.种类]) + Decimal(e.金额))
        for row in fund_by_settlement.values():
            net = Decimal(row["暂缓"]) + Decimal(row["追回"]) - Decimal(row["恢复"])
            row["净挂账"] = _money(net)
        return {
            "召回编号": recall.编号,
            "范围版本": recall.版本号,
            "数量守恒": balanced,
            "范围总重量kg": str(scope_weight),
            "去向": {"销毁kg": str(destroyed), "退回kg": str(returned),
                    "替代交付kg": str(replaced), "解除kg": str(released),
                    "仍隔离kg": str(in_quarantine),
                    "已交代合计kg": str(decided)},
            "未回执节点": no_receipt,
            "未终态节点": open_tasks,
            "出围冻结节点": [self._task_view(t) for t in out_held],
            "待裁决冲突": [c for c in recall.冲突编号
                          if self._conflicts[c].状态 == "待授权复核"],
            "资金变化": [self._fund_view(e) for e in funds],
            "资金按结算汇总": list(fund_by_settlement.values()),
        }

    # ------------------------------------------------------------------ 召回查询与权限

    def list_recalls(self, actor: Actor) -> list[dict]:
        if actor.role == ROLE_GUARD:
            raise PermissionDenied("查看召回")
        with self._lock:
            out = []
            for recall in self._recalls.values():
                if actor.role == ROLE_FARMER and recall.农户编号 != actor.farmer_id \
                        and not self._recall_touches_farmer(recall, actor.farmer_id):
                    continue
                if actor.role == ROLE_ENTERPRISE \
                        and not self._recall_touches_enterprise(recall, actor):
                    continue
                if actor.role == ROLE_PROCESSOR \
                        and not self._recall_touches_processor(recall, actor):
                    continue
                out.append(self._recall_summary(recall, actor))
            return out

    def get_recall(self, actor: Actor, recall_id: str) -> dict:
        with self._lock:
            recall = self._recalls.get(recall_id)
            if recall is None:
                raise NotFound("召回单")
            self._assert_recall_visible(recall, actor)
            return self._scoped_recall_view(recall, actor)

    def _assert_recall_visible(self, recall: Recall, actor: Actor):
        if actor.role == ROLE_GUARD:
            raise PermissionDenied("查看召回")
        if actor.role == ROLE_FARMER:
            if recall.农户编号 != actor.farmer_id and \
                    not self._recall_touches_farmer(recall, actor.farmer_id):
                raise PermissionDenied("查看他人召回")
        elif actor.role == ROLE_ENTERPRISE:
            if not self._recall_touches_enterprise(recall, actor):
                raise PermissionDenied("查看非本企业相关召回")
        elif actor.role == ROLE_PROCESSOR:
            if not self._recall_touches_processor(recall, actor):
                raise PermissionDenied("查看非本加工方相关召回")

    def _recall_touches_farmer(self, recall: Recall, farmer_id: str) -> bool:
        return any(self._recall_tasks[t].农户编号 == farmer_id
                   for t in recall.任务编号)

    def _task_enterprise(self, task: RecallTask) -> str | None:
        """解析任务所属企业：交货/退货看交货单；成品看其加工批次登记的企业。"""
        if task.节点类型 == "交货单":
            return self._deliveries[task.节点编号].企业编号
        if task.节点类型 == "退货单":
            did = self._returns[task.节点编号].交货单编号
            return self._deliveries[did].企业编号
        if task.节点类型 == "成品":
            pb = self._proc_batches[self._goods[task.节点编号].加工批次编号]
            return pb.企业编号
        return None

    def _recall_touches_enterprise(self, recall: Recall, actor: Actor) -> bool:
        for t in recall.任务编号:
            task = self._recall_tasks[t]
            if task.保管方 in {"企业", "承运方"} and \
                    self._task_enterprise(task) == actor.name:
                return True
        return False

    def _recall_touches_processor(self, recall: Recall, actor: Actor) -> bool:
        # 仅当该加工方是某些任务的当前保管方（到厂原料、交付前成品）才可见；
        # 成品交付客户后保管方转为企业，加工方不再可见。
        return any(self._recall_tasks[t].保管方 == "加工方"
                   and self._task_processor(self._recall_tasks[t]) == actor.name
                   for t in recall.任务编号)

    def _load_task_for_keeper(self, actor: Actor, task_id: str) -> RecallTask:
        if actor.role == ROLE_GUARD:
            raise PermissionDenied("处置召回货物")
        task = self._recall_tasks.get(task_id)
        if task is None:
            raise NotFound("召回任务")
        if actor.role in {ROLE_REGULATOR, ROLE_COOP}:
            return task
        allowed = CUSTODY_ROLES.get(task.保管方, set())
        if actor.role not in allowed:
            raise PermissionDenied("处置非本保管方货物")
        # 企业只能处置本企业链条上的货
        if actor.role == ROLE_ENTERPRISE:
            if not self._task_belongs_enterprise(task, actor.name):
                raise PermissionDenied("处置非本企业货物")
        # 加工方只能处置登记在自己名下的到厂原料或自产成品
        if actor.role == ROLE_PROCESSOR:
            owner = self._task_processor(task)
            if owner and owner != actor.name:
                raise PermissionDenied("处置非本加工方货物")
        return task

    def _task_processor(self, task: RecallTask) -> str:
        if task.节点类型 == "成品":
            return self._proc_batches[self._goods[task.节点编号]
                                      .加工批次编号].加工方编号
        if task.节点类型 == "交货单":
            return self._deliveries[task.节点编号].加工方编号
        return ""

    def _task_belongs_enterprise(self, task: RecallTask, enterprise: str) -> bool:
        return self._task_enterprise(task) == enterprise

    # ------------------------------------------------------------------ 召回序列化

    def _pesticide_view(self, p: PesticideReview) -> dict:
        return {"农残编号": p.编号, "来源检验编号": p.来源检验编号,
                "批次编号": p.批次编号, "检验时间": p.检验时间,
                "检验员": p.检验员, "项目": p.项目, "结果": p.结果,
                "实测值": p.实测值, "限量值": p.限量值,
                "判定依据": p.判定依据, "样本编号": p.样本编号,
                "复核自": p.复核自, "现行": p.现行}

    def _proc_batch_view(self, pb: ProcessingBatch) -> dict:
        return {"加工批次编号": pb.编号, "企业编号": pb.企业编号,
                "加工方编号": pb.加工方编号, "时间": pb.时间, "制品": pb.制品,
                "原料行": pb.原料行, "成品": pb.成品}

    def _goods_view(self, g: FinishedGood) -> dict:
        return {"成品编号": g.编号, "加工批次编号": g.加工批次编号,
                "制品": g.制品, "重量kg": str(g.重量kg), "时间": g.时间,
                "保管方": g.保管方, "物流状态": g.物流状态}

    def _task_view(self, t: RecallTask) -> dict:
        return {"任务编号": t.编号, "召回编号": t.召回编号,
                "节点类型": t.节点类型, "节点编号": t.节点编号,
                "位置": t.位置, "保管方": t.保管方,
                "任务重量kg": str(t.重量kg), "状态": t.状态,
                "隔离方式": t.隔离方式,
                "隔离中kg": str(t.隔离中kg), "已销毁kg": str(t.已销毁kg),
                "已退回kg": str(t.已退回kg), "已替代kg": str(t.已替代kg),
                "已解除kg": str(t.已解除kg),
                "未处置kg": str(t.未处置kg()),
                "农户编号": t.农户编号, "记录": t.记录}

    def _receipt_view(self, r: DisposalReceipt) -> dict:
        return {"回执编号": r.编号, "任务编号": r.任务编号,
                "召回编号": r.召回编号, "动作": r.动作,
                "重量kg": str(r.重量kg), "时间": r.时间, "回执人": r.回执人,
                "离线补录": r.离线补录, "备注": r.备注}

    def _notice_view(self, n: RecallNotice) -> dict:
        return {"通知编号": n.编号, "任务编号": n.任务编号,
                "召回编号": n.召回编号, "时间": n.时间,
                "渠道": n.渠道, "离线": n.离线}

    def _conflict_view(self, c: RecallConflict) -> dict:
        return {"冲突编号": c.编号, "任务编号": c.任务编号,
                "召回编号": c.召回编号, "上报内容": c.上报内容,
                "上报人": c.上报人, "时间": c.时间, "状态": c.状态,
                "裁决人": c.裁决人, "裁决时间": c.裁决时间, "裁决意见": c.裁决意见}

    def _fund_view(self, e: RecallFundEntry) -> dict:
        return {"追加账编号": e.编号, "召回编号": e.召回编号,
                "结算编号": e.结算编号, "农户编号": e.农户编号,
                "种类": e.种类, "金额": e.金额, "时间": e.时间,
                "摘要": e.摘要, "关联追回编号": e.关联追回编号}

    def _version_view(self, v: RecallVersion) -> dict:
        return {"版本": v.版本, "时间": v.时间, "操作": v.操作,
                "节点类型": v.节点类型, "节点编号": v.节点编号,
                "范围内": v.范围内, "新增": v.新增, "移出": v.移出, "备注": v.备注}

    def _recall_summary(self, recall: Recall, actor: Actor) -> dict:
        return {"召回编号": recall.编号, "发起时间": recall.发起时间,
                "发起人": recall.发起人, "起点类型": recall.起点类型,
                "起点编号": recall.起点编号, "异常依据编号": recall.异常依据编号,
                "版本号": recall.版本号, "状态": recall.状态,
                "任务数": len(recall.任务编号),
                "待回执": sum(1 for t in recall.任务编号
                              if self._recall_tasks[t].状态 in {"待通知", "已通知"})}

    def _scoped_recall_view(self, recall: Recall, actor: Actor,
                            report: bool = False) -> dict:
        view = self._recall_view(recall, actor)
        if report:
            view["结案报告"] = self._build_report(recall)
        return view

    def _recall_view(self, recall: Recall, actor: Actor) -> dict:
        view = {
            "召回编号": recall.编号, "发起时间": recall.发起时间,
            "发起人": recall.发起人, "起点类型": recall.起点类型,
            "起点编号": recall.起点编号, "异常依据编号": recall.异常依据编号,
            "状态": recall.状态, "结案时间": recall.结案时间,
            "范围版本": [self._version_view(v) for v in recall.版本],
            "任务": [self._task_view(self._recall_tasks[t])
                     for t in recall.任务编号],
        }
        # 通知/回执/冲突：任务相关方可见与自己相关的；监管/合作社看全量
        if actor.role in {ROLE_REGULATOR, ROLE_COOP}:
            view["通知"] = [self._notice_view(self._notices[n])
                            for n in recall.通知编号]
            view["回执"] = [self._receipt_view(self._receipts[r])
                            for r in recall.回执编号]
            view["冲突"] = [self._conflict_view(self._conflicts[c])
                            for c in recall.冲突编号]
            view["追加账"] = [self._fund_view(self._funds[f])
                              for f in recall.追加账编号]
        else:
            task_ids = self._visible_task_ids(recall, actor)
            view["任务"] = [t for t in view["任务"] if t["任务编号"] in task_ids]
            view["通知"] = [self._notice_view(self._notices[n])
                            for n in recall.通知编号
                            if self._notices[n].任务编号 in task_ids]
            view["回执"] = [self._receipt_view(self._receipts[r])
                            for r in recall.回执编号
                            if self._receipts[r].任务编号 in task_ids]
            view["冲突"] = [self._conflict_view(self._conflicts[c])
                            for c in recall.冲突编号
                            if self._conflicts[c].任务编号 in task_ids]
            if actor.role == ROLE_FARMER:
                # 农户只见本人追加账，不见企业/加工节点全貌
                view["追加账"] = [self._fund_view(self._funds[f])
                                  for f in recall.追加账编号
                                  if self._funds[f].农户编号 == actor.farmer_id]
                view["任务"] = [t for t in view["任务"]
                                if t.get("农户编号") == actor.farmer_id]
                view["通知"] = [n for n in view["通知"]
                                if n["任务编号"] in {t["任务编号"] for t in view["任务"]}]
                view["回执"] = [r for r in view["回执"]
                                if r["任务编号"] in {t["任务编号"] for t in view["任务"]}]
                view["冲突"] = [c for c in view["冲突"]
                                if c["任务编号"] in {t["任务编号"] for t in view["任务"]}]
            else:
                view["追加账"] = []
        if recall.结案报告 is not None:
            view["结案报告"] = recall.结案报告
        return view

    def _visible_task_ids(self, recall: Recall, actor: Actor) -> set[str]:
        ids = set()
        for tid in recall.任务编号:
            task = self._recall_tasks[tid]
            if actor.role == ROLE_ENTERPRISE and \
                    task.保管方 in {"企业", "承运方"} and \
                    self._task_belongs_enterprise(task, actor.name):
                ids.add(tid)
            if actor.role == ROLE_PROCESSOR and task.保管方 == "加工方" and \
                    self._task_processor(task) == actor.name:
                ids.add(tid)
            if actor.role == ROLE_FARMER and task.农户编号 == actor.farmer_id:
                ids.add(tid)
        return ids

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
                "核定毛重kg": str(d.毛重kg),
                "在库kg": str(d.在库kg), "在途kg": str(d.在途kg),
                "到厂kg": str(d.到厂kg)}

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
