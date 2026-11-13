import {ReportResult,Cell} from '../types/domain';import {DataTable} from './common/DataTable';import {Dimensions} from '../constants/enums';import {number,metricValue} from '../utils/format';
function MetricCell({cell,metricKey,ratio}:{cell:Cell;metricKey:string;ratio:boolean}){
  const v=metricValue(cell,metricKey,ratio?4:2);
  if(v.uncomputable)return <td className="uncomputable" title={v.reason}><b>不可计算</b><small>{v.reason}</small></td>;
  return <td>{v.text}</td>;
}
export function PivotTable({result}:{result:ReportResult}){return <DataTable head={[...result.dimensions.map(d=>Dimensions[d as keyof typeof Dimensions]),...result.metrics.map(m=>m.name),'对照差异 / 元','明细数']}>{result.cells.map((cell,i)=><tr key={i}>{result.dimensions.map(d=><td key={d}>{cell.dimensions[d]}</td>)}{result.metrics.map(m=><MetricCell key={m.key} cell={cell} metricKey={m.key} ratio={m.unit==='比率'}/>)}<td className={Math.abs(cell.difference)>.01?'difference':''}>{number(cell.difference)}</td><td>{cell.source_count}</td></tr>)}<tr className="total-row"><td colSpan={result.dimensions.length}>重算合计</td>{result.metrics.map(m=><MetricCell key={m.key} cell={result.total} metricKey={m.key} ratio={m.unit==='比率'}/>)}<td className="difference">{number(result.total.difference)}</td><td>{result.total.source_count}</td></tr></DataTable>}
