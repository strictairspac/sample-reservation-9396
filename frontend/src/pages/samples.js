import {table} from '../components/common/table.js';
import {esc} from '../utils/html.js';
import {session} from '../hooks/session.js';
export function samplesPage(items,summary){
  const mode=summary?.mode==='on';
  const rows=(summary?.samples)||items;
  const canWrite=session.user?.role!=='viewer';
  const stockCell=s=>canWrite
    ?`<div class="stock-edit"><input type="number" min="1" max="100000" value="${s.quantity}" data-stock="${s.id}" aria-label="${esc(s.code)} 库存"><button class="mini" data-stock-save="${s.id}">调整</button></div>`
    :`<b class="quantity">${s.quantity}</b> 份`;
  return `<div class="split"><section><div class="section-top"><h2>待移交样本</h2><span>${items.length} 批 / 台账${mode?' · 在线预留已启用':''}</span></div>${table(['样本编号 / 批次','库存','已预留','可用量','当前位置'],rows.map(s=>[`<strong>${esc(s.code)}</strong><small>${esc(s.title)}</small>`,stockCell(s),`${s.reserved??0} 份`,`${s.available??s.quantity} 份`,esc(s.location)]))}<div class="notice">台账保留真实库存；每次并发演练复制可用量到独立 PostgreSQL 行，不影响业务原件。${mode?'预留中的份数仍计入库存但不可再次申请；确认扣库存后库存才下调，撤销会释放可用量。库存下调不得低于已预留合计。':''}</div></section><section class="form-panel"><span class="eyebrow">REGISTER SAMPLE</span><h2>登记新样本批次</h2><form id="sample-form"><label>批次编号<input name="code" placeholder="QY-261117-D" required></label><label>样本名称<input name="title" placeholder="地表水复测留样" required></label><div class="fields"><label>可用份数<input name="quantity" type="number" min="1" max="100000" value="100" required></label><label>存放位置<input name="location" value="冷藏间 A-09" required></label></div><button class="primary" ${canWrite?'':'disabled'}>登记样本 +</button></form><p class="help">登记后到「流转申请」指定接收实验室及移交份数。${mode?'预留期间可在左侧直接调整库存，下调不得低于已预留合计。':''}</p></section></div>`;}
