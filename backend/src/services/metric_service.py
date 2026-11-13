from collections import defaultdict
from fastapi import HTTPException
from ..models import Metric,SourceRow
from ..constants.dimensions import BASE_FIELDS
from ..utils.serialize import record
from .expression_service import compile_metrics,calculate
from .pivot_service import build_report
from .audit_service import log
def _strict_trial(sources,prospective,dimensions):
    """登记前严格试算：现有任一渠道/月或区域/月单元出现零分母即 422 拒绝。
    报表生成阶段则容错，只把相关指标标记为不可计算。"""
    trees,order,_=compile_metrics(prospective)
    grouped=defaultdict(list)
    for row in sources:
        grouped[tuple(row.day[:7] if dim=="month" else getattr(row,dim) for dim in dimensions)].append(row)
    for members in list(grouped.values())+[sources]:
        base={key:sum(getattr(r,key) for r in members) for key in BASE_FIELDS}
        calculate(base,trees,order)
def list_metrics(db):
    rows=db.query(Metric).order_by(Metric.id).all()
    _,order,deps=compile_metrics(rows)
    return {"items":[dict(record(m),dependencies=deps[m.key]) for m in rows],"evaluation_order":order}
def create_metric(db,data,user):
    if db.query(Metric).filter_by(key=data.key).first():raise HTTPException(409,"指标标识已存在")
    row=Metric(**data.model_dump(),owner=user.name)
    try:
        prospective=db.query(Metric).order_by(Metric.id).all()+[row]
        compile_metrics(prospective)
        sources=db.query(SourceRow).order_by(SourceRow.id).all()
        if sources:
            # 容错构建确认报表结构可生成；严格试算保证现有数据不存在零分母
            build_report(sources,prospective,["channel","month"])
            build_report(sources,prospective,["region","month"])
            _strict_trial(sources,prospective,["channel","month"])
            _strict_trial(sources,prospective,["region","month"])
    except ValueError as e:raise HTTPException(422,str(e))
    db.add(row);db.flush();log(db,user.email,"metric.register",row.key);db.commit()
    return record(row)
