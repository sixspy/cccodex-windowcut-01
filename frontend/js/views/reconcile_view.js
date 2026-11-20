export const reconcileViewTemplate = `
<div class="card wide" v-show="tab===4">
  <h2>物化对账</h2>
  <p>
    窗内事件 {{recon.window_events}} / 金额 {{recon.window_amount}} ·
    迟到桶 {{recon.late_bucket}} · 拒收 {{recon.rejects}} ·
    待放行暂存 {{recon.hold_pending}} / 金额 {{recon.hold_pending_amount}} ·
    封口 {{recon.seal_ledger ? recon.seal_ledger.status : '—' }} ·
    封口偏了 {{recon.seal_deviations}} ·
    对齐 <b :class="recon.aligned?'ok':'late'">{{recon.aligned}}</b>
  </p>
  <table>
    <thead><tr><th>窗口</th><th>状态</th><th>窗内笔数</th><th>迟到</th><th>待放行</th><th>封口</th><th>偏了</th><th>对齐</th></tr></thead>
    <tbody>
      <tr v-for="r in reconAll" :key="r.window_id">
        <td>{{r.name}}</td>
        <td>{{r.status}}</td>
        <td>{{r.window_events}}</td>
        <td>{{r.late_bucket}}</td>
        <td>{{r.hold_pending}}</td>
        <td>{{r.seal_status || '—'}}</td>
        <td :class="r.seal_deviations?'late':''">{{r.seal_deviations}}</td>
        <td :class="r.aligned?'ok':'late'">{{r.aligned}}</td>
      </tr>
    </tbody>
  </table>
</div>
`;
