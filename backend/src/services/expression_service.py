"""Restricted arithmetic AST with dependency DAG. No eval/exec or dynamic imports."""
import ast,math
from ..constants.dimensions import BASE_FIELDS
UNCOMPUTABLE=None  # 不可计算占位：与真实零值（0）严格区分，序列化后为 JSON null
ALLOWED=(ast.Expression,ast.BinOp,ast.UnaryOp,ast.Name,ast.Load,ast.Constant,ast.Add,ast.Sub,ast.Mult,ast.Div,ast.UAdd,ast.USub)
def parse(expression):
    try:tree=ast.parse(expression,mode="eval")
    except SyntaxError:raise ValueError("表达式语法不正确")
    nodes=list(ast.walk(tree))
    if len(nodes)>50:raise ValueError("表达式最多50个语法节点")
    for node in nodes:
        if not isinstance(node,ALLOWED):raise ValueError("仅支持字段、数值和加减乘除，禁止函数调用或属性访问")
        if isinstance(node,ast.Constant) and (type(node.value) not in (int,float) or abs(node.value)>1e9 or not math.isfinite(node.value)):raise ValueError("数值常量必须有限且不超过1e9")
    return tree,{n.id for n in nodes if isinstance(n,ast.Name)}
def compile_metrics(metrics):
    mapping={m.key:m for m in metrics}
    if set(mapping)&BASE_FIELDS:raise ValueError("指标名称不能覆盖来源字段")
    trees={};deps={}
    for key,m in mapping.items():
        tree,names=parse(m.expression)
        missing=names-set(mapping)-BASE_FIELDS
        if missing:raise ValueError("未知依赖："+", ".join(sorted(missing)))
        trees[key]=tree;deps[key]=names & set(mapping)
    order=[];visiting=set();done=set()
    def visit(key):
        if key in visiting:raise ValueError("指标依赖存在环路："+key)
        if key in done:return
        visiting.add(key)
        for dep in sorted(deps[key]):visit(dep)
        visiting.remove(key);done.add(key);order.append(key)
    for key in mapping:visit(key)
    return trees,order,{key:sorted(value) for key,value in deps.items()}
def evaluate_node(node,values):
    if isinstance(node,ast.Expression):return evaluate_node(node.body,values)
    if isinstance(node,ast.Constant):return float(node.value)
    if isinstance(node,ast.Name):return values[node.id]
    if isinstance(node,ast.UnaryOp):
        val=evaluate_node(node.operand,values)
        return -val if isinstance(node.op,ast.USub) else val
    a=evaluate_node(node.left,values);b=evaluate_node(node.right,values)
    if isinstance(node.op,ast.Add):value=a+b
    elif isinstance(node.op,ast.Sub):value=a-b
    elif isinstance(node.op,ast.Mult):value=a*b
    else:
        if abs(b)<1e-12:raise ValueError("指标表达式分母为零")
        value=a/b
    if not math.isfinite(value) or abs(value)>1e18:raise ValueError("指标计算结果超出有限范围")
    return value
def calculate(base,trees,order):
    """严格模式：任一指标零分母或越界即抛错。用于口径登记前的现有数据试算。"""
    values=dict(base)
    for key in order:
        try:values[key]=evaluate_node(trees[key],values)
        except ValueError as e:raise ValueError(key+"："+str(e))
    return {key:round(values[key],6) for key in order}
def _evaluate_tolerant(node,values):
    if isinstance(node,ast.Expression):return _evaluate_tolerant(node.body,values)
    if isinstance(node,ast.Constant):return float(node.value)
    if isinstance(node,ast.Name):
        value=values[node.id]
        if value is UNCOMPUTABLE:raise KeyError(node.id)  # 上游指标不可计算，沿依赖传播
        return value
    if isinstance(node,ast.UnaryOp):
        val=_evaluate_tolerant(node.operand,values)
        return -val if isinstance(node.op,ast.USub) else val
    a=_evaluate_tolerant(node.left,values);b=_evaluate_tolerant(node.right,values)
    if isinstance(node.op,ast.Add):value=a+b
    elif isinstance(node.op,ast.Sub):value=a-b
    elif isinstance(node.op,ast.Mult):value=a*b
    else:
        if abs(b)<1e-12:raise ValueError("指标表达式分母为零")
        value=a/b
    if not math.isfinite(value) or abs(value)>1e18:raise ValueError("指标计算结果超出有限范围")
    return value
def calculate_tolerant(base,trees,order):
    """容错模式：按 DAG 顺序逐指标计算，零分母/越界只让该指标及下游不可计算，
    其余指标与全部来源字段照常给出。返回 (values,reasons)，不可计算值为 None，
    真实零值保持 0，绝不以零填充不可计算单元。"""
    values=dict(base);reasons={}
    for key in order:
        try:values[key]=_evaluate_tolerant(trees[key],values)
        except ValueError as e:reasons[key]=str(e)
        except KeyError as e:reasons[key]="依赖指标 "+e.args[0]+" 不可计算"
        if key in reasons:values[key]=UNCOMPUTABLE
    result={}
    for key in order:
        if key in reasons:result[key]=UNCOMPUTABLE
        else:result[key]=round(values[key],6)
    return result,reasons
