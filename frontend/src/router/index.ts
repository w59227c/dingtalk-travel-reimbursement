import { createRouter, createWebHistory } from 'vue-router'

import { pinia } from '@/pinia'
import { useAuthStore } from '@/stores/auth'
import ReimburseView from '@/views/ReimburseView.vue'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
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
      meta: { requiresAdmin: true },
    },
    {
      path: '/m/settings',
      name: 'mobile-admin-settings',
      component: () => import('@/views/SettingsAdminView.vue'),
      props: { mobile: true },
      meta: { requiresAdmin: true },
    },
  ],
})

router.beforeEach(async (to) => {
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
  return true
})

export default router
