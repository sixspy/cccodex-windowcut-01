/** 业务窗页签模板片段 */
export const windowsViewTemplate = `
<div class="card wide" v-show="tab===0">
  <h2>窗口条带</h2>
  <p class="muted">关窗占窗锁；后到争用只记审计。关窗后才能物化。</p>
  <p class="muted">
    封口分流册：<b>{{routing.path_label || routing.path || '未定'}}</b>
    · 当天 {{routing.biz_date || '—'}} 一锤定音，禁止改口
  </p>
  <table>
    <thead><tr><th>窗口</th><th>世代</th><th>开启</th><th>关闭</th><th>状态</th><th>封口台账</th><th>操作</th></tr></thead>
    <tbody>
      <tr v-for="w in windows" :key="w.id">
        <td>{{w.name}}</td>
        <td>{{w.generation}}</td>
        <td>{{w.open_at}}</td>
        <td>{{w.close_at}}</td>
        <td :class="w.status==='封口中'?'sealing':''">{{w.status}}</td>
        <td>
          <span v-if="w.seal_ledger">{{w.seal_ledger.status}} · 快照 {{w.seal_ledger.snapshot_count}} · 暂存 {{w.seal_ledger.held_count}}</span>
          <span v-else class="muted">—</span>
        </td>
        <td>
          <button class="ghost" @click="closeWin(w.id)">关窗</button>
          <button class="ghost" @click="materialize(w.id)">物化</button>
          <button class="ghost" @click="loadRecon(w.id)">对账</button>
          <button class="ghost" @click="reopenStub(w.id)">重开空桩</button>
        </td>
      </tr>
    </tbody>
  </table>
  <div class="err">{{actionErr}}</div>
</div>
`;
