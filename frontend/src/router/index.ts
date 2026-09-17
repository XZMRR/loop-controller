import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/login',
      name: 'Login',
      component: () => import('@/views/Login.vue'),
      meta: { public: true },
    },
    {
      path: '/',
      component: () => import('@/components/Layout.vue'),
      redirect: '/dashboard',
      children: [
        {
          path: 'dashboard',
          name: 'Dashboard',
          component: () => import('@/views/Dashboard.vue'),
          meta: { title: '仪表盘' },
        },
        {
          path: 'approvals',
          name: 'Approvals',
          component: () => import('@/views/Approvals.vue'),
          meta: { title: '审批台' },
        },
        {
          path: 'agents',
          name: 'Agents',
          component: () => import('@/views/Agents.vue'),
          meta: { title: 'Agent 管理' },
        },
        {
          path: 'tools',
          name: 'Tools',
          component: () => import('@/views/Tools.vue'),
          meta: { title: '工具策略' },
        },
        {
          path: 'debug',
          name: 'Debug',
          component: () => import('@/views/Debug.vue'),
          meta: { title: '判定调试' },
        },
        {
          path: 'audit',
          name: 'Audit',
          component: () => import('@/views/Audit.vue'),
          meta: { title: '审计查询' },
        },
        {
          path: 'settings',
          name: 'Settings',
          component: () => import('@/views/Settings.vue'),
          meta: { title: '系统配置' },
        },
        {
          path: 'a2a',
          name: 'A2A',
          component: () => import('@/views/A2A.vue'),
          meta: { title: 'A2A 治理' },
        },
        {
          path: 'dead-letters',
          name: 'DeadLetters',
          component: () => import('@/views/DeadLetters.vue'),
          meta: { title: '死信队列' },
        },
      ],
    },
    {
      path: '/:pathMatch(.*)*',
      redirect: '/',
    },
  ],
})

router.beforeEach((to, _from, next) => {
  const auth = useAuthStore()
  if (!to.meta.public && !auth.isAuthenticated) {
    next('/login')
  } else if (to.path === '/login' && auth.isAuthenticated) {
    next('/')
  } else {
    next()
  }
})

export default router
