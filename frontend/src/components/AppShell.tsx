import { useEffect, useRef, useState } from 'react'
import { Link, NavLink, Outlet, useLocation } from 'react-router-dom'
import { useLogout, useMe } from '../auth/useAuth'
import { CENTRE_NAME } from '../config'
import { NAV_ITEMS } from '../nav'
import { CommandPalette } from './CommandPalette'

const isMac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform)

export function AppShell() {
  const me = useMe()
  const logout = useLogout()
  const location = useLocation()
  // Menus are tied to the page they were opened on, so they close on navigation.
  const [navOpenAt, setNavOpenAt] = useState<string | null>(null)
  const [menuOpenAt, setMenuOpenAt] = useState<string | null>(null)
  const navOpen = navOpenAt === location.pathname
  const menuOpen = menuOpenAt === location.pathname
  const setNavOpen = (fn: (v: boolean) => boolean) => setNavOpenAt(fn(navOpen) ? location.pathname : null)
  const setMenuOpen = (open: boolean | ((v: boolean) => boolean)) =>
    setMenuOpenAt((typeof open === 'function' ? open(menuOpen) : open) ? location.pathname : null)
  const [paletteOpen, setPaletteOpen] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)
  const toggleRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setPaletteOpen(true)
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])

  useEffect(() => {
    if (!menuOpen) return
    const onDown = (e: MouseEvent) => {
      if (!menuRef.current?.contains(e.target as Node)) setMenuOpenAt(null)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setMenuOpenAt(null)
        toggleRef.current?.focus()
      }
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [menuOpen])

  const user = me.data
  if (!user) return null
  const items = NAV_ITEMS.filter((i) => !i.adminOnly || user.role === 'admin')
  const mainItems = items.filter((i) => !i.adminOnly)
  const adminItems = items.filter((i) => i.adminOnly)
  const roleLabel = user.role === 'admin' ? 'Admin' : 'Employee'

  const renderLinks = (list: typeof items) => (
    <ul>
      {list.map((i) => (
        <li key={i.to}>
          <NavLink to={i.to} end>
            {i.label}
          </NavLink>
        </li>
      ))}
    </ul>
  )

  return (
    <>
    <div className="shell" inert={paletteOpen}>
      <a href="#main" className="skip-link">
        Skip to main content
      </a>
      <header className="header">
        <button
          type="button"
          className="btn nav-toggle"
          aria-expanded={navOpen}
          aria-controls="main-nav"
          onClick={() => setNavOpen((v) => !v)}
        >
          Menu
        </button>
        <span className="centre-name">{CENTRE_NAME}</span>
        <button type="button" className="btn" onClick={() => setPaletteOpen(true)}>
          Search<span className="visually-hidden"> customers, jobs and vehicles</span>
          <small className="shortcut-hint" aria-hidden="true">{isMac ? '⌘K' : 'Ctrl+K'}</small>
        </button>
        <div className="user-menu" ref={menuRef}>
          <button
            type="button"
            className="btn"
            aria-expanded={menuOpen}
            aria-controls="user-menu"
            ref={toggleRef}
            onClick={() => setMenuOpen((v) => !v)}
          >
            <span className="user-label">{user.name}</span>
            <span className="role-label"> ({roleLabel})</span>
          </button>
          {menuOpen && (
            <div className="menu-list" id="user-menu">
              <p className="hint" style={{ margin: '0 8px' }}>
                Signed in as {user.name} ({roleLabel})
              </p>
              <Link className="btn" to="/change-password">
                Change password
              </Link>
              <button type="button" className="btn" disabled={logout.isPending} onClick={() => logout.mutate()}>
                Sign out
              </button>
            </div>
          )}
        </div>
      </header>
      <div className="shell-body">
        <nav id="main-nav" className="nav" aria-label="Main" data-open={navOpen}>
          {renderLinks(mainItems)}
          {adminItems.length > 0 && (
            <>
              <p className="nav-heading">Admin</p>
              {renderLinks(adminItems)}
            </>
          )}
        </nav>
        <main id="main" className="content" tabIndex={-1}>
          <Outlet />
        </main>
      </div>
    </div>
    {paletteOpen && <CommandPalette onClose={() => setPaletteOpen(false)} />}
    </>
  )
}
