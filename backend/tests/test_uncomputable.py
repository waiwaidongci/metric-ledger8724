"""零分母容错、依赖传播与合计独立重算的验证。

运行：cd backend && python3 -m tests.test_uncomputable
仅验证纯计算逻辑，对 fastapi/redis 等第三方依赖做轻量 stub，无需数据库或 Redis。
"""
import csv
import io
import sys
import types
import importlib.util
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


def _install_stubs():
    """在无第三方依赖的环境里也能导入被测服务。"""
    fastapi = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code=400, detail=""):
            super().__init__(detail)
            self.status_code, self.detail = status_code, detail

    fastapi.HTTPException = HTTPException
    sys.modules.setdefault("fastapi", fastapi)

    redis_mod = types.ModuleType("redis")

    class _Redis:
        @classmethod
        def from_url(cls, *a, **k):
            return cls()

    redis_mod.Redis = _Redis
    sys.modules.setdefault("redis", redis_mod)


def _load_report_service():
    """report_service 顶部会经 models 引入 SQLAlchemy；测试只覆盖其 CSV 纯函数，
    因此用最小桩包隔离加载，避免要求本地安装完整后端依赖。"""
    pkg = types.ModuleType("rs_stub")
    pkg.__path__ = []
    sys.modules["rs_stub"] = pkg
    config = types.ModuleType("rs_stub.config")
    settings = types.ModuleType("rs_stub.config.settings")
    settings.REDIS_URL = "redis://stub/0"
    config.settings = settings
    models = types.ModuleType("rs_stub.models")

    class ReportRun:  # 导出测试不触达 ORM，仅占位满足导入
        pass

    models.ReportRun = ReportRun
    utils = types.ModuleType("rs_stub.utils")
    serialize = types.ModuleType("rs_stub.utils.serialize")
    serialize.record = lambda r: r
    services = types.ModuleType("rs_stub.services")
    audit = types.ModuleType("rs_stub.services.audit_service")
    audit.log = lambda *a, **k: None
    sys.modules.update({
        "rs_stub.config": config,
        "rs_stub.config.settings": settings,
        "rs_stub.models": models,
        "rs_stub.utils": utils,
        "rs_stub.utils.serialize": serialize,
        "rs_stub.services": services,
        "rs_stub.services.audit_service": audit,
    })
    spec = importlib.util.spec_from_file_location(
        "rs_stub.services.report_service", BACKEND / "src/services/report_service.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_install_stubs()

from src.services import expression_service as expr
from src.services.pivot_service import build_report

report_service = _load_report_service()


class FakeMetric:
    def __init__(self, key, name, expression, unit="元"):
        self.key, self.name, self.expression, self.unit = key, name, expression, unit


class FakeRow:
    def __init__(self, source_key, day, channel, region, revenue, refunds, cost, orders, reference_net):
        self.source_key, self.day, self.channel, self.region = source_key, day, channel, region
        self.revenue, self.refunds, self.cost, self.orders = revenue, refunds, cost, orders
        self.reference_net = reference_net


SEED_METRICS = [
    FakeMetric("net_revenue", "净收入", "revenue-refunds"),
    FakeMetric("gross_profit", "贡献毛利", "net_revenue-cost"),
    FakeMetric("margin_rate", "贡献毛利率", "gross_profit/net_revenue", "比率"),
    FakeMetric("net_per_order", "每单净收入", "net_revenue/orders", "元/单"),
    FakeMetric("refund_rate", "退款率", "refunds/revenue", "比率"),
]


def row(key, channel, month, revenue, refunds, cost, orders, reference=None):
    return FakeRow(key, f"{month}-10", channel, "华东", revenue, refunds, cost, orders,
                   reference if reference is not None else round(revenue - refunds, 2))


def no_order_channel_rows():
    # 数字发行 2026-10 整月无订单（orders=0）；其他渠道正常
    return [
        row("HC-1", "直营渠道", "2026-10", 1000.0, 50.0, 400.0, 20),
        row("HC-2", "书店合作", "2026-10", 800.0, 0.0, 300.0, 10),
        row("HC-3", "数字发行", "2026-10", 600.0, 30.0, 200.0, 0),
        row("HC-4", "数字发行", "2026-09", 500.0, 10.0, 150.0, 8),
    ]


def cell_by(result, channel, month):
    return next(c for c in result["cells"] if c["dimensions"] == {"channel": channel, "month": month})


def test_zero_denominator_marks_metric_not_fails():
    result = build_report(no_order_channel_rows(), SEED_METRICS, ["channel", "month"])
    bad = cell_by(result, "数字发行", "2026-10")
    # 报表整体成功，问题单元的金额类指标与可算字段全部保留
    assert bad["values"]["net_revenue"] == 570.0
    assert bad["values"]["gross_profit"] == 370.0
    assert bad["reference_net"] == 570.0 and bad["source_count"] == 1
    # 零分母指标不可计算且附原因，值是 None（JSON null）而不是 0
    assert bad["values"]["net_per_order"] is None
    assert "分母为零" in bad["uncomputable"]["net_per_order"]
    # 真实零值仍为 0（书店合作 refunds=0 → 退款率真实为 0），与不可计算严格区分
    zero = cell_by(result, "书店合作", "2026-10")
    assert zero["values"]["refund_rate"] == 0.0
    assert "refund_rate" not in zero["uncomputable"]
    print("ok 零分母单元：可算指标保留，不可计算指标不填零并附原因")


def test_dependency_propagation():
    trees, order, _ = expr.compile_metrics(SEED_METRICS)
    # 构造净收入为零的单元：margin_rate 零分母，应标记不可计算
    base = {"revenue": 100.0, "refunds": 100.0, "cost": 20.0, "orders": 5}
    values, reasons = expr.calculate_tolerant(base, trees, order)
    assert values["net_revenue"] == 0.0
    assert values["gross_profit"] == -20.0
    assert values["margin_rate"] is None
    assert "分母为零" in reasons["margin_rate"]
    # 增加一个显式依赖 margin_rate 的指标，验证下游传播
    extra = SEED_METRICS + [FakeMetric("margin_per_order", "单均毛利贡献", "margin_rate/orders", "比率")]
    trees2, order2, _ = expr.compile_metrics(extra)
    values2, reasons2 = expr.calculate_tolerant(base, trees2, order2)
    assert values2["margin_per_order"] is None
    assert "margin_rate" in reasons2["margin_per_order"] and "不可计算" in reasons2["margin_per_order"]
    # 同单元不相关指标照常计算
    assert values2["net_per_order"] == 0.0
    print("ok 依赖传播：零分母指标的下游全部不可计算并标注依赖原因，其余指标不受影响")


def test_total_recomputed_independently():
    result = build_report(no_order_channel_rows(), SEED_METRICS, ["channel", "month"])
    total = result["total"]
    # 合计按全部来源重算：总订单 38（含无订单单元），分母非零 → 每单净收入可算
    assert total["source_count"] == 4
    total_net = 1000 - 50 + 800 + 600 - 30 + 500 - 10  # 2810
    assert total["values"]["net_revenue"] == round(total_net, 6)
    assert total["values"]["net_per_order"] == round(total_net / 38, 6)
    assert "net_per_order" not in total["uncomputable"]
    # 其他渠道单元完全不受问题单元影响
    good = cell_by(result, "直营渠道", "2026-10")
    assert good["values"]["net_per_order"] == round(950.0 / 20, 6)
    assert not good["uncomputable"]
    print("ok 合计独立重算：全部来源汇总后可算，其他渠道单元不受影响")


def test_total_itself_uncomputable_when_all_orders_zero():
    rows = [
        row("HC-1", "直营渠道", "2026-10", 1000.0, 50.0, 400.0, 0),
        row("HC-2", "数字发行", "2026-10", 600.0, 30.0, 200.0, 0),
    ]
    result = build_report(rows, SEED_METRICS, ["channel", "month"])
    total = result["total"]
    # 合计分母本身为零：同样标记不可计算，绝不填零，金额仍保留
    assert total["values"]["net_per_order"] is None
    assert "分母为零" in total["uncomputable"]["net_per_order"]
    assert total["values"]["net_revenue"] == 1520.0
    assert total["canonical_net"] == 1520.0
    print("ok 合计零分母：合计自身不可计算时同样标记并保留金额")


def test_strict_mode_still_rejects_for_registration():
    trees, order, _ = expr.compile_metrics(SEED_METRICS)
    base = {"revenue": 100.0, "refunds": 0.0, "cost": 20.0, "orders": 0}
    try:
        expr.calculate(base, trees, order)
    except ValueError as e:
        assert "net_per_order" in str(e) and "分母为零" in str(e)
    else:
        raise AssertionError("严格试算必须对零分母抛错，用于口径登记 422")
    print("ok 严格模式：登记试算仍会拒绝零分母定义")


def test_export_distinguishes_uncomputable_from_zero():
    result = build_report(no_order_channel_rows(), SEED_METRICS, ["channel", "month"])
    # 直接复用导出内部的行渲染，避免触碰数据库
    dims, keys = result["dimensions"], result["evaluation_order"]
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(dims + keys + ["reference_net", "difference", "source_count"])
    for cell in result["cells"]:
        w.writerow(report_service._row_text(cell, dims, keys))
    text = out.getvalue()
    assert "不可计算" in text and "分母为零" in text
    # 真实零值行里退款率导出为 0，而不是不可计算
    zero_line = [line for line in text.splitlines() if line.startswith("书店合作")][0]
    zero_cells = next(csv.reader([zero_line]))
    assert zero_cells[keys.index("refund_rate") + len(dims)] == "0"
    bad_line = [line for line in text.splitlines() if line.startswith("数字发行,2026-10")][0]
    bad_cells = next(csv.reader([bad_line]))
    assert bad_cells[keys.index("net_per_order") + len(dims)].startswith("不可计算")
    print("ok CSV 导出：不可计算写出原因，真实零值写出 0")


def test_export_total_row_uses_independent_total():
    result = build_report(no_order_channel_rows(), SEED_METRICS, ["channel", "month"])
    total = result["total"]
    assert report_service._cell_text(total, "net_per_order") == ("%.6f" % (2810 / 38)).rstrip("0").rstrip(".")
    assert report_service._cell_text(total, "net_revenue") == "2810"
    print("ok 导出合计：重算合计值独立渲染，不沿用问题单元")


def test_json_persistence_keeps_null_distinct_from_zero():
    # worker 把 result 写入 JSON 列与 /artifacts 文件：None 必须序列化为 null，
    # 重新读回后仍是 None（不可计算），而真实 0 仍是 0
    import json
    result = build_report(no_order_channel_rows(), SEED_METRICS, ["channel", "month"])
    reloaded = json.loads(json.dumps(result, ensure_ascii=False))
    bad = cell_by(reloaded, "数字发行", "2026-10")
    zero = cell_by(reloaded, "书店合作", "2026-10")
    assert bad["values"]["net_per_order"] is None
    assert bad["uncomputable"]["net_per_order"]
    assert zero["values"]["refund_rate"] == 0
    print("ok JSON 持久化：不可计算为 null，真实零值为 0，归档与交付快照可区分")


def test_boundary_guards_unchanged():
    try:
        build_report([], SEED_METRICS, ["channel", "month"])
    except ValueError as e:
        assert "没有可计算" in str(e)
    else:
        raise AssertionError("空来源仍须明确失败")
    try:
        build_report(no_order_channel_rows(), SEED_METRICS, ["channel", "channel"])
    except ValueError:
        pass
    else:
        raise AssertionError("重复维度仍须拒绝")
    print("ok 边界守卫：空来源与非法维度的既有校验不变")


if __name__ == "__main__":
    test_zero_denominator_marks_metric_not_fails()
    test_dependency_propagation()
    test_total_recomputed_independently()
    test_total_itself_uncomputable_when_all_orders_zero()
    test_strict_mode_still_rejects_for_registration()
    test_export_distinguishes_uncomputable_from_zero()
    test_export_total_row_uses_independent_total()
    test_json_persistence_keeps_null_distinct_from_zero()
    test_boundary_guards_unchanged()
    print("\n全部验证通过：零分母容错、依赖传播、合计独立重算、导出区分零值。")
