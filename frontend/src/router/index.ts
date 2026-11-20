import { createRouter, createWebHistory } from 'vue-router'

import { pinia } from '@/pinia'
import { useAuthStore } from '@/stores/auth'
import ReimburseView from '@/views/ReimburseView.vue'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  scrollBehavior(to) {
    if (['#oa-template-catalog', '#general-oa-template'].includes(to.hash)) return { el: '#oa-template-catalog', top: 16 }
  },
  routes: [
    {
      path: '/',
      name: 'reimburse',
      component: ReimburseView,
    },
    {
      path: '/m',
      name: 'mobile-reimburse',
      component: ReimburseView,
      props: { mobile: true },
    },
    {
      path: '/admin/settings',
      name: 'admin-settings',
      component: () => import('@/views/SettingsAdminView.vue'),
      props: (route) => ({ initialTemplateTab: route.hash === '#general-oa-template' ? 'general' : 'travel' }),
      meta: { requiresAdmin: true },
    },
    {
      path: '/m/settings',
      name: 'mobile-admin-settings',
      component: () => import('@/views/SettingsAdminView.vue'),
      props: (route) => ({ mobile: true, initialTemplateTab: route.hash === '#general-oa-template' ? 'general' : 'travel' }),
      meta: { requiresAdmin: true },
    },
  ],
})

router.beforeEach(async (to, from) => {
  const auth = useAuthStore(pinia)
  const existingSession = !!auth.session
  await auth.bootstrap()
  if (to.meta.requiresAdmin && existingSession) {
    // Settings may have granted or revoked this user's role since bootstrap.
    // Do not authorize an admin page from the cached client-side flag alone.
    try { await auth.refreshMe() }
    catch { return { name: to.name === 'mobile-admin-settings' ? 'mobile-reimburse' : 'reimburse' } }
  }
  if (to.meta.requiresAdmin && !auth.isAdmin) {
    return { name: to.name === 'mobile-admin-settings' ? 'mobile-reimburse' : 'reimburse' }
  }
  if (from.meta.requiresAdmin && auth.session?.operator && (to.name === 'reimburse' || to.name === 'mobile-reimburse')) {
    // Recheck the delegation after settings, including an administrator's own revoked grant.
    try { await auth.refreshMe() }
    catch { /* The reimbursement page checks scope when loading the workspace and surfaces failures. */ }
  }
  return true
})

export default router
