import { AppShell, Code, Group, Stack, Text, Title } from '@mantine/core'
import { useQuery } from '@tanstack/react-query'

interface Health {
  status: string
}

export function App() {
  const health = useQuery<Health>({
    queryKey: ['health'],
    queryFn: async () => {
      const res = await fetch('/healthz')
      if (!res.ok) throw new Error('unhealthy')
      return res.json()
    },
  })

  return (
    <AppShell header={{ height: 56 }} padding="md">
      <AppShell.Header>
        <Group h="100%" px="md">
          <Title order={4}>AC Server Manager</Title>
        </Group>
      </AppShell.Header>
      <AppShell.Main>
        <Stack>
          <Text>Phase 0 scaffold is running.</Text>
          <Text>
            Backend health:{' '}
            <Code>{health.isLoading ? '…' : (health.data?.status ?? 'error')}</Code>
          </Text>
        </Stack>
      </AppShell.Main>
    </AppShell>
  )
}
