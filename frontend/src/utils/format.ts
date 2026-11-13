export const number=(n:number|undefined|null,d=2)=>n===undefined?'—':n===null?'不可计算':n.toLocaleString('zh-CN',{minimumFractionDigits:d,maximumFractionDigits:d});
export const money=(n:number|undefined|null)=>n===null?'不可计算':'¥ '+number(n);
export const percent=(n:number|null|undefined)=>n==null?'—':number(n*100,2)+'%';
export const time=(s:string)=>new Date(s).toLocaleString('zh-CN',{hour12:false});
// 真实零值（0）与不可计算（null）严格区分：不可计算单元展示原因，不填零
export const metricValue=(cell:{values:Record<string,number|null>;uncomputable?:Record<string,string>},key:string,d=2)=>{
  const v=cell.values[key];
  if(v===null)return {text:'不可计算',reason:cell.uncomputable?.[key]||'缺少可计算输入',uncomputable:true};
  return {text:number(v,d),reason:'',uncomputable:false};
};
