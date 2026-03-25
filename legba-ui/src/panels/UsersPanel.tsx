import { useState, useEffect } from 'react'
import {
  useUsers,
  useCreateUser,
  useUpdateUser,
  useDeleteUser,
  useChangePassword,
} from '@/api/hooks'
import { Shield, UserPlus, Trash2, KeyRound, Check, AlertCircle, Lock } from 'lucide-react'
import { cn } from '@/lib/utils'

interface User {
  id: string
  username: string
  role: string
  created_at: string | null
  last_login: string | null
}

const ROLES = ['admin', 'analyst', 'viewer'] as const

function roleBadge(role: string) {
  switch (role) {
    case 'admin':
      return 'bg-red-500/20 text-red-400'
    case 'analyst':
      return 'bg-blue-500/20 text-blue-400'
    case 'viewer':
      return 'bg-emerald-500/20 text-emerald-400'
    default:
      return 'bg-secondary text-muted-foreground'
  }
}

function formatDate(iso: string | null) {
  if (!iso) return '--'
  try {
    return new Date(iso).toLocaleString(undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return iso
  }
}

const inputClass =
  'w-full px-2 py-1 text-sm bg-secondary border border-border rounded focus:outline-none focus:ring-1 focus:ring-primary'

export function UsersPanel() {
  // --- Current user password change ---
  const [curPw, setCurPw] = useState('')
  const [newPw, setNewPw] = useState('')
  const [confirmPw, setConfirmPw] = useState('')
  const [pwStatus, setPwStatus] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle')
  const [pwError, setPwError] = useState('')

  // --- Create user form ---
  const [showCreate, setShowCreate] = useState(false)
  const [newUsername, setNewUsername] = useState('')
  const [newUserPw, setNewUserPw] = useState('')
  const [newUserRole, setNewUserRole] = useState<string>('viewer')
  const [createStatus, setCreateStatus] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle')
  const [createError, setCreateError] = useState('')

  // --- Reset password state ---
  const [resetId, setResetId] = useState<string | null>(null)
  const [resetPw, setResetPw] = useState('')

  // --- Inline role edit state ---
  const [editRoleId, setEditRoleId] = useState<string | null>(null)
  const [editRoleValue, setEditRoleValue] = useState<string>('')

  // --- Current user (fetched from /me) ---
  const [currentUsername, setCurrentUsername] = useState<string | null>(null)

  useEffect(() => {
    fetch('/api/v2/auth/me')
      .then((r) => r.json())
      .then((d) => setCurrentUsername(d?.user?.username ?? null))
      .catch(() => {})
  }, [])

  const { data, isLoading } = useUsers()
  const createUserMut = useCreateUser()
  const updateUserMut = useUpdateUser()
  const deleteUserMut = useDeleteUser()
  const changePwMut = useChangePassword()

  const users: User[] = data?.users ?? []

  // --- Handlers ---

  function handleChangePassword() {
    setPwError('')
    if (!newPw || !curPw) {
      setPwError('All fields are required')
      return
    }
    if (newPw !== confirmPw) {
      setPwError('New passwords do not match')
      return
    }
    if (newPw.length < 8) {
      setPwError('Password must be at least 8 characters')
      return
    }
    setPwStatus('saving')
    changePwMut.mutate(
      { current_password: curPw, new_password: newPw },
      {
        onSuccess: () => {
          setPwStatus('saved')
          setCurPw('')
          setNewPw('')
          setConfirmPw('')
          setTimeout(() => setPwStatus('idle'), 2500)
        },
        onError: (err: any) => {
          setPwStatus('error')
          setPwError(err?.message?.includes('incorrect') ? 'Current password is incorrect' : 'Failed to change password')
          setTimeout(() => setPwStatus('idle'), 3000)
        },
      },
    )
  }

  function handleCreateUser() {
    setCreateError('')
    if (!newUsername.trim() || !newUserPw) {
      setCreateError('Username and password are required')
      return
    }
    if (newUserPw.length < 8) {
      setCreateError('Password must be at least 8 characters')
      return
    }
    setCreateStatus('saving')
    createUserMut.mutate(
      { username: newUsername.trim(), password: newUserPw, role: newUserRole },
      {
        onSuccess: () => {
          setCreateStatus('saved')
          setNewUsername('')
          setNewUserPw('')
          setNewUserRole('viewer')
          setTimeout(() => {
            setCreateStatus('idle')
            setShowCreate(false)
          }, 1500)
        },
        onError: (err: any) => {
          setCreateStatus('error')
          setCreateError(err?.message?.includes('exists') ? 'Username already exists' : 'Failed to create user')
          setTimeout(() => setCreateStatus('idle'), 3000)
        },
      },
    )
  }

  function handleRoleChange(userId: string) {
    if (!editRoleValue) return
    updateUserMut.mutate(
      { userId, body: { role: editRoleValue } },
      {
        onSuccess: () => setEditRoleId(null),
      },
    )
  }

  function handleResetPassword(userId: string) {
    if (!resetPw || resetPw.length < 8) return
    updateUserMut.mutate(
      { userId, body: { password: resetPw } },
      {
        onSuccess: () => {
          setResetId(null)
          setResetPw('')
        },
      },
    )
  }

  function handleDeleteUser(userId: string, username: string) {
    if (!confirm(`Delete user "${username}"? This cannot be undone.`)) return
    deleteUserMut.mutate(userId)
  }

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center gap-2 px-4 py-2.5 border-b border-border shrink-0">
        <Shield size={16} className="text-primary" />
        <h2 className="text-sm font-semibold text-foreground">User Management</h2>
      </div>

      <div className="flex-1 overflow-y-auto p-4 space-y-6">
        {/* --- Change own password --- */}
        <div className="border border-border rounded-lg p-4">
          <div className="flex items-center gap-2 mb-3">
            <Lock size={14} className="text-muted-foreground" />
            <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              Change Your Password
            </h3>
          </div>
          <div className="flex items-end gap-3 flex-wrap">
            <div className="flex-1 min-w-[150px]">
              <label className="text-xs text-muted-foreground block mb-1">Current Password</label>
              <input
                type="password"
                value={curPw}
                onChange={(e) => setCurPw(e.target.value)}
                className={inputClass}
                placeholder="Current password"
              />
            </div>
            <div className="flex-1 min-w-[150px]">
              <label className="text-xs text-muted-foreground block mb-1">New Password</label>
              <input
                type="password"
                value={newPw}
                onChange={(e) => setNewPw(e.target.value)}
                className={inputClass}
                placeholder="New password (min 8 chars)"
              />
            </div>
            <div className="flex-1 min-w-[150px]">
              <label className="text-xs text-muted-foreground block mb-1">Confirm</label>
              <input
                type="password"
                value={confirmPw}
                onChange={(e) => setConfirmPw(e.target.value)}
                className={inputClass}
                placeholder="Confirm new password"
              />
            </div>
            <button
              onClick={handleChangePassword}
              disabled={pwStatus === 'saving'}
              className="px-3 py-1 rounded text-sm bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50 shrink-0"
            >
              {pwStatus === 'saving' ? 'Saving...' : 'Save'}
            </button>
          </div>
          {pwStatus === 'saved' && (
            <div className="flex items-center gap-1 mt-2 text-xs text-emerald-400">
              <Check size={12} /> Password updated
            </div>
          )}
          {pwError && (
            <div className="flex items-center gap-1 mt-2 text-xs text-destructive">
              <AlertCircle size={12} /> {pwError}
            </div>
          )}
        </div>

        {/* --- Users table --- */}
        <div>
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              All Users
            </h3>
            <button
              onClick={() => setShowCreate((v) => !v)}
              className="flex items-center gap-1 px-2 py-1 rounded text-xs bg-primary text-primary-foreground hover:bg-primary/90"
            >
              <UserPlus size={12} />
              {showCreate ? 'Cancel' : 'New User'}
            </button>
          </div>

          {/* Create user form */}
          {showCreate && (
            <div className="border border-border rounded-lg p-3 mb-4 bg-secondary/30">
              <div className="flex items-end gap-3 flex-wrap">
                <div className="flex-1 min-w-[140px]">
                  <label className="text-xs text-muted-foreground block mb-1">Username</label>
                  <input
                    type="text"
                    value={newUsername}
                    onChange={(e) => setNewUsername(e.target.value)}
                    className={inputClass}
                    placeholder="Username"
                  />
                </div>
                <div className="flex-1 min-w-[140px]">
                  <label className="text-xs text-muted-foreground block mb-1">Password</label>
                  <input
                    type="password"
                    value={newUserPw}
                    onChange={(e) => setNewUserPw(e.target.value)}
                    className={inputClass}
                    placeholder="Min 8 characters"
                  />
                </div>
                <div className="w-28">
                  <label className="text-xs text-muted-foreground block mb-1">Role</label>
                  <select
                    value={newUserRole}
                    onChange={(e) => setNewUserRole(e.target.value)}
                    className={inputClass}
                  >
                    {ROLES.map((r) => (
                      <option key={r} value={r}>
                        {r}
                      </option>
                    ))}
                  </select>
                </div>
                <button
                  onClick={handleCreateUser}
                  disabled={createStatus === 'saving'}
                  className="px-3 py-1 rounded text-sm bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50 shrink-0"
                >
                  {createStatus === 'saving' ? 'Creating...' : 'Create'}
                </button>
              </div>
              {createStatus === 'saved' && (
                <div className="flex items-center gap-1 mt-2 text-xs text-emerald-400">
                  <Check size={12} /> User created
                </div>
              )}
              {createError && (
                <div className="flex items-center gap-1 mt-2 text-xs text-destructive">
                  <AlertCircle size={12} /> {createError}
                </div>
              )}
            </div>
          )}

          {/* Table */}
          {isLoading ? (
            <div className="text-sm text-muted-foreground">Loading...</div>
          ) : users.length === 0 ? (
            <div className="text-sm text-muted-foreground">No users found</div>
          ) : (
            <div className="border border-border rounded-lg overflow-hidden">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border bg-secondary/40">
                    <th className="text-left px-3 py-2 text-xs font-semibold text-muted-foreground uppercase tracking-wider">
                      Username
                    </th>
                    <th className="text-left px-3 py-2 text-xs font-semibold text-muted-foreground uppercase tracking-wider">
                      Role
                    </th>
                    <th className="text-left px-3 py-2 text-xs font-semibold text-muted-foreground uppercase tracking-wider">
                      Created
                    </th>
                    <th className="text-left px-3 py-2 text-xs font-semibold text-muted-foreground uppercase tracking-wider">
                      Last Login
                    </th>
                    <th className="text-right px-3 py-2 text-xs font-semibold text-muted-foreground uppercase tracking-wider">
                      Actions
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {users.map((u) => {
                    const isSelf = u.username === currentUsername
                    return (
                      <tr
                        key={u.id}
                        className="border-b border-border/50 hover:bg-secondary/20 transition-colors"
                      >
                        <td className="px-3 py-2 text-foreground font-medium">
                          {u.username}
                          {isSelf && (
                            <span className="ml-1.5 text-[10px] text-muted-foreground">(you)</span>
                          )}
                        </td>
                        <td className="px-3 py-2">
                          {editRoleId === u.id ? (
                            <div className="flex items-center gap-1">
                              <select
                                value={editRoleValue}
                                onChange={(e) => setEditRoleValue(e.target.value)}
                                className="px-1 py-0.5 text-xs bg-secondary border border-border rounded"
                              >
                                {ROLES.map((r) => (
                                  <option key={r} value={r}>
                                    {r}
                                  </option>
                                ))}
                              </select>
                              <button
                                onClick={() => handleRoleChange(u.id)}
                                className="p-0.5 text-emerald-400 hover:text-emerald-300"
                                title="Save role"
                              >
                                <Check size={12} />
                              </button>
                              <button
                                onClick={() => setEditRoleId(null)}
                                className="p-0.5 text-muted-foreground hover:text-foreground"
                                title="Cancel"
                              >
                                &times;
                              </button>
                            </div>
                          ) : (
                            <button
                              onClick={() => {
                                setEditRoleId(u.id)
                                setEditRoleValue(u.role)
                              }}
                              className="group"
                              title="Click to change role"
                            >
                              <span
                                className={cn(
                                  'inline-block px-1.5 py-0.5 rounded text-[11px] font-medium uppercase',
                                  roleBadge(u.role),
                                )}
                              >
                                {u.role}
                              </span>
                            </button>
                          )}
                        </td>
                        <td className="px-3 py-2 text-muted-foreground text-xs">
                          {formatDate(u.created_at)}
                        </td>
                        <td className="px-3 py-2 text-muted-foreground text-xs">
                          {formatDate(u.last_login)}
                        </td>
                        <td className="px-3 py-2 text-right">
                          <div className="flex items-center gap-1 justify-end">
                            {/* Reset password */}
                            {resetId === u.id ? (
                              <div className="flex items-center gap-1">
                                <input
                                  type="password"
                                  value={resetPw}
                                  onChange={(e) => setResetPw(e.target.value)}
                                  className="w-32 px-1.5 py-0.5 text-xs bg-secondary border border-border rounded"
                                  placeholder="New password"
                                />
                                <button
                                  onClick={() => handleResetPassword(u.id)}
                                  disabled={resetPw.length < 8}
                                  className="p-0.5 text-emerald-400 hover:text-emerald-300 disabled:opacity-30"
                                  title="Set password"
                                >
                                  <Check size={12} />
                                </button>
                                <button
                                  onClick={() => {
                                    setResetId(null)
                                    setResetPw('')
                                  }}
                                  className="p-0.5 text-muted-foreground hover:text-foreground"
                                  title="Cancel"
                                >
                                  &times;
                                </button>
                              </div>
                            ) : (
                              <button
                                onClick={() => {
                                  setResetId(u.id)
                                  setResetPw('')
                                }}
                                className="p-1 rounded hover:bg-secondary text-muted-foreground hover:text-foreground transition-colors"
                                title="Reset password"
                              >
                                <KeyRound size={13} />
                              </button>
                            )}

                            {/* Delete */}
                            <button
                              onClick={() => handleDeleteUser(u.id, u.username)}
                              disabled={isSelf}
                              className={cn(
                                'p-1 rounded transition-colors',
                                isSelf
                                  ? 'text-muted-foreground/30 cursor-not-allowed'
                                  : 'hover:bg-secondary text-muted-foreground hover:text-destructive',
                              )}
                              title={isSelf ? 'Cannot delete yourself' : 'Delete user'}
                            >
                              <Trash2 size={13} />
                            </button>
                          </div>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
