import { Route, Routes } from 'react-router-dom'
import { AppShell } from './components/AppShell'
import { Placeholder } from './components/Placeholder'
import { ApiEvents, RequireAdmin, RequireAuth } from './auth/Guards'
import { LoginPage } from './pages/LoginPage'
import { ChangePasswordPage } from './pages/ChangePasswordPage'
import { DashboardPage } from './pages/DashboardPage'
import { NotFoundPage } from './pages/NotFoundPage'

const PLACEHOLDERS: { path: string; title: string }[] = [
  { path: 'customers', title: 'Customers' },
  { path: 'customers/:id', title: 'Customer' },
  { path: 'jobs/new', title: 'New Job' },
  { path: 'jobs', title: 'Jobs' },
  { path: 'jobs/:id', title: 'Job' },
  { path: 'documents', title: 'Documents' },
  { path: 'vehicles', title: 'Vehicles & Expiries' },
  { path: 'vehicles/:id', title: 'Vehicle' },
  { path: 'receipts', title: 'Receipts' },
  { path: 'reports', title: 'Reports & Exports' },
]
const ADMIN_PLACEHOLDERS: { path: string; title: string }[] = [
  { path: 'services', title: 'Services' },
  { path: 'users', title: 'Users' },
  { path: 'audit-log', title: 'Audit log' },
  { path: 'background', title: 'Background work' },
]

export function AppRoutes() {
  return (
    <>
      <ApiEvents />
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route element={<RequireAuth allowForcedChange />}>
          <Route path="/change-password" element={<ChangePasswordPage />} />
        </Route>
        <Route element={<RequireAuth />}>
          <Route element={<AppShell />}>
            <Route index element={<DashboardPage />} />
            {PLACEHOLDERS.map((p) => (
              <Route key={p.path} path={p.path} element={<Placeholder title={p.title} />} />
            ))}
            <Route element={<RequireAdmin />}>
              {ADMIN_PLACEHOLDERS.map((p) => (
                <Route key={p.path} path={p.path} element={<Placeholder title={p.title} />} />
              ))}
            </Route>
            <Route path="*" element={<NotFoundPage />} />
          </Route>
        </Route>
      </Routes>
    </>
  )
}
