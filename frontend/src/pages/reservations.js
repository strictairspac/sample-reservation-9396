import {table} from '../components/common/table.js';
import {badge} from '../components/common/badge.js';
import {esc,date} from '../utils/html.js';
import {session} from '../hooks/session.js';

const canWrite=()=>session.user?.role!=='viewer';

function runStatus(run){
  if(!run)return '未启用';
  return badge(run.status);
}

function runPanel(summary){
  const run=summary.run;
  if(!run)return `<div class="notice">尚未启用预留流程：库存调整与申请登记照常进行，启用后全部已有申请将按原编号一次性纳入预留。</div>`;
  const failures=Array.isArray(run.result?.shortages)?run.result.shortages:[];
  const shortageTable=failures.length?table(['样本','库存','已有申请','缺口'],failures.map(x=>[esc(x.sample_code),`${x.quantity} 份`,`${x.requested} 份`,`缺 ${x.missing} 份`])):'';
  const detail=run.status==='failed'
    ?`<div class="error">失败原因：${esc(run.error||'未知错误')}。修正库存或申请后，以同一编号、同一内容重试即可恢复。</div>${shortageTable}${canWrite()?`<button class="mini" data-retry="${run.id}">以原编号重试启用 ↻</button>`:''}`
    :run.status==='processing'
      ?`<div class="notice">启用处理中：本页每 1.5 秒刷新。此时申请登记与库存调整照常完成，启用会重新核对全量数据。${run.concurrent_wait?'另一请求正在处理该批次，已返回最近一次状态。':''}${canWrite()?` <button class="mini" data-retry="${run.id}">继续/恢复启用 ↻</button>`:''}</div>`
      :run.status==='pending'
        ?`<div class="notice">待启用：批次已登记但尚未完成切换。${canWrite()?`<button class="mini" data-retry="${run.id}">继续/恢复启用 ↻</button>`:'可稍候刷新。'} 中断后重试会重新核对全量申请，只补缺失的预留。</div>`
        :`<div class="notice">已生效：${run.application_count} 笔申请按原编号全量预留，无遗漏、无重复占量。可逐笔确认扣库存或撤销释放预留。</div>`;
  return `<div class="reservation-status ${esc(run.status)}"><div><span class="eyebrow">ENABLEMENT BATCH / ${esc(run.code)}</span><h2>${runStatus(run)} <small>${date(run.created_at)} · ${esc(run.created_by)}</small></h2></div><div class="reservation-meta"><span>状态：${esc(run.status_label||run.status)}</span><span>纳入申请：${run.application_count} 笔</span><span>预留：${run.reserved_count} 笔</span></div></div>${detail}`;
}

function reservationRows(items){
  return items.map(r=>{
    const action=r.status==='reserved'&&canWrite()
      ?`<div class="row-actions"><button class="mini" data-confirm="${r.id}">确认扣库存</button><button class="mini ghost" data-cancel="${r.id}">撤销预留</button></div>`
      :`<small>${r.status==='reserved'?'等待办理':'终态，不可再变更'}</small>`;
    return [`<strong>${esc(r.code)}</strong><small>${esc(r.sample_code)} → ${esc(r.destination)}</small>`,`${r.quantity} 份`,badge(r.status),action];
  });
}

export function reservationsPage(summary,items){
  const samples=summary.samples||[];
  const counts=summary.counts||{};
  const modeLabel=summary.mode==='on'?'在线预留已启用':'在线预留未启用';
  return `<div class="split"><section><div class="section-top"><h2>预留流程启用</h2><span>在线启用 · ${esc(modeLabel)}</span></div>${runPanel(summary)}<div class="section-top"><h2>预留明细</h2><span>${counts.reserved||0} 预留中 / ${counts.confirmed||0} 已确认 / ${counts.cancelled||0} 已撤销</span></div>${items.length?table(['申请编号 / 样本','预留份数','状态','办理'],reservationRows(items)):'<div class="empty">还没有预留记录。启用预留流程后，全部已有申请会按原编号生成预留。</div>'}<div class="section-top"><h2>启用期间台账</h2><span>库存调整与申请登记照常完成</span></div>${table(['样本','库存','已预留','可用量'],samples.map(s=>[`<strong>${esc(s.code)}</strong>`, `${s.quantity} 份`,`${s.reserved} 份`,`<b class="quantity">${s.available}</b> 份`]))}<div class="protocol"><h3>启用协议</h3><ol><li>任一申请超出所属样本库存则整批不生效，并列出缺口</li><li>修正后可同编号同内容重试；改内容将被拒绝</li><li>启用切换与申请登记串行：切换前的申请由批次纳入，切换后的申请在线预留</li><li>确认与撤销并发仅一个终态；库存下调不会让可用量为负</li></ol></div></section><section class="form-panel"><span class="eyebrow">ONLINE ENABLEMENT</span><h2>启用预留流程</h2><form id="reservation-form"><label>启用批次编号<input name="code" placeholder="RSV-20261117" value="RSV-20261117" required></label><label>启用说明<input name="note" value="10:30 交接班启用" maxlength="300"></label><label>内容确认<select name="content"><option value="full">按原编号全量纳入现有申请</option></select></label><button class="primary" ${canWrite()?'':'disabled'}>启用预留流程 ↗</button></form><p class="help">启用中断后可恢复：同编号同内容重试会继续未完成批次，不会重复占量；同编号改内容返回 409。</p><div class="help"><b>办理规则</b><br>确认扣库存：库存减预留量，预留转为已确认。<br>撤销预留：释放预留量，申请保留但不再占量。<br>两者并发仅一个成功，另一笔收到冲突提示。</div></section></div>`;
}
