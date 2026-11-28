export interface NavItem {
  to: string
  label: string
  adminOnly?: boolean
}

export const NAV_ITEMS: NavItem[] = [
  { to: '/', label: 'Dashboard' },
  { to: '/customers', label: 'Customers' },
  { to: '/jobs/new', label: 'New Job' },
  { to: '/jobs', label: 'Jobs' },
  { to: '/documents', label: 'Documents' },
  { to: '/vehicles', label: 'Vehicles & Expiries' },
  { to: '/receipts', label: 'Receipts' },
  { to: '/reports', label: 'Reports & Exports' },
  { to: '/services', label: 'Services', adminOnly: true },
  { to: '/users', label: 'Users', adminOnly: true },
  { to: '/audit-log', label: 'Audit log', adminOnly: true },
  { to: '/background', label: 'Background work', adminOnly: true },
]
