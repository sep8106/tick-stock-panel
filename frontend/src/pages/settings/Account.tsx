/**
 * 账号安全面板 — 查看当前登录账号、修改密码、退出登录。
 *
 * 改密码成功后后端会清空全部会话(含当前), 因此直接跳回登录页。
 */
import { useState, type FormEvent } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Loader2, LogOut, ShieldAlert, ShieldCheck, User } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { PageHeader } from '@/components/PageHeader'

const INPUT_CLS = 'h-9 w-full rounded-btn border border-border bg-base px-3 text-sm text-foreground outline-none transition-colors focus:border-accent/50'

function goLogin() {
  window.location.href = '/login'
}

export function SettingsAccountPanel() {
  const statusQ = useQuery({ queryKey: QK.authStatus, queryFn: api.authStatus })
  const [oldPwd, setOldPwd] = useState('')
  const [newPwd, setNewPwd] = useState('')
  const [confirmPwd, setConfirmPwd] = useState('')
  const [error, setError] = useState('')

  const changeM = useMutation({
    mutationFn: () => api.authChangePassword(oldPwd, newPwd),
    onSuccess: goLogin,
    onError: (e: Error) => setError(e.message || '修改失败'),
  })

  const logoutM = useMutation({
    mutationFn: api.authLogout,
    onSettled: goLogin,
  })

  const submit = (e: FormEvent) => {
    e.preventDefault()
    setError('')
    if (newPwd.length < 6) { setError('新密码至少 6 位'); return }
    if (newPwd !== confirmPwd) { setError('两次输入的新密码不一致'); return }
    changeM.mutate()
  }

  const status = statusQ.data

  return (
    <>
      <PageHeader title="账号安全" subtitle="登录账号、修改密码与退出登录" />

      {statusQ.isLoading ? (
        <div className="flex justify-center py-10"><Loader2 className="h-5 w-5 animate-spin text-muted" /></div>
      ) : statusQ.isError ? (
        <div className="rounded-card border border-border bg-surface p-5 text-sm text-danger">
          读取登录状态失败: {(statusQ.error as Error).message}
        </div>
      ) : !status?.configured ? (
        <section className="rounded-card border border-border bg-surface p-5">
          <div className="flex items-center gap-2 mb-2">
            <ShieldAlert className="h-4 w-4 text-warning" />
            <h3 className="text-sm font-medium text-foreground">尚未启用登录</h3>
          </div>
          <p className="text-[11px] leading-relaxed text-muted">
            当前未设置登录账号, 仅本机/内网可访问面板。
          </p>
          <button
            type="button"
            onClick={goLogin}
            className="mt-3 inline-flex h-8 items-center rounded-btn bg-accent px-3 text-xs font-medium text-white hover:bg-accent/90"
          >
            去设置账号密码
          </button>
        </section>
      ) : (
        <>
          <section className="rounded-card border border-border bg-surface p-5">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex items-center gap-2.5">
                <div className="grid h-9 w-9 place-items-center rounded-lg bg-accent/15 text-accent">
                  <User className="h-5 w-5" />
                </div>
                <div>
                  <div className="text-sm font-medium text-foreground">{status.username ?? '—'}</div>
                  <div className="text-[11px] text-muted">当前登录账号</div>
                </div>
              </div>
              <button
                type="button"
                onClick={() => logoutM.mutate()}
                disabled={logoutM.isPending}
                className="inline-flex h-8 items-center gap-1.5 rounded-btn border border-border px-3 text-xs text-foreground hover:border-danger/50 hover:text-danger disabled:opacity-50"
              >
                {logoutM.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <LogOut className="h-3.5 w-3.5" />}
                退出登录
              </button>
            </div>
          </section>

          <section className="rounded-card border border-border bg-surface p-5 mt-6">
            <div className="flex items-center gap-2 mb-4">
              <ShieldCheck className="h-4 w-4 text-accent" />
              <h3 className="text-sm font-medium text-foreground">修改密码</h3>
            </div>
            <form onSubmit={submit} className="max-w-sm space-y-3">
              {/* 供密码管理器关联账号 */}
              <input type="text" value={status.username ?? ''} autoComplete="username" readOnly hidden />
              <input type="password" value={oldPwd} onChange={e => setOldPwd(e.target.value)} placeholder="当前密码" autoComplete="current-password" className={INPUT_CLS} />
              <input type="password" value={newPwd} onChange={e => setNewPwd(e.target.value)} placeholder="新密码(至少 6 位)" autoComplete="new-password" className={INPUT_CLS} />
              <input type="password" value={confirmPwd} onChange={e => setConfirmPwd(e.target.value)} placeholder="再次输入新密码" autoComplete="new-password" className={INPUT_CLS} />
              {error && (
                <div className="flex items-start gap-1.5 rounded-btn bg-danger/10 px-3 py-2 text-[11px] text-danger">
                  <ShieldAlert className="mt-px h-3.5 w-3.5 shrink-0" />
                  <span>{error}</span>
                </div>
              )}
              <div className="flex items-center gap-3">
                <button
                  type="submit"
                  disabled={changeM.isPending || !oldPwd || !newPwd || !confirmPwd}
                  className="inline-flex h-8 items-center gap-1.5 rounded-btn bg-accent px-3 text-xs font-medium text-white hover:bg-accent/90 disabled:opacity-50"
                >
                  {changeM.isPending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                  修改密码
                </button>
                <span className="text-[10px] text-muted">修改后所有设备需重新登录</span>
              </div>
            </form>
          </section>
        </>
      )}
    </>
  )
}
