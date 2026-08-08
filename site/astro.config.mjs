import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

export default defineConfig({
  site: 'https://ericfinland.github.io',
  base: '/hearth-windows',
  integrations: [
    starlight({
      title: 'hearth for Windows',
      description:
        'Run local LLMs and an autonomous coding agent on your own Windows machine. One install, no terminal, no cloud.',
      logo: { src: './src/assets/logo.svg', alt: 'hearth flame logo' },
      favicon: '/favicon.svg',
      social: {
        github: 'https://github.com/EricFinland/hearth-windows',
      },
      editLink: {
        baseUrl: 'https://github.com/EricFinland/hearth-windows/edit/main/site/',
      },
      lastUpdated: true,
      components: {
        Footer: './src/components/Footer.astro',
      },
      customCss: ['./src/styles/theme.css'],
      head: [
        { tag: 'link', attrs: { rel: 'preconnect', href: 'https://fonts.googleapis.com' } },
        { tag: 'link', attrs: { rel: 'preconnect', href: 'https://fonts.gstatic.com', crossorigin: true } },
        {
          tag: 'link',
          attrs: {
            rel: 'stylesheet',
            href: 'https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Newsreader:opsz,wght@6..72,400;6..72,500;6..72,600&display=swap',
          },
        },
        { tag: 'meta', attrs: { property: 'og:image', content: 'https://ericfinland.github.io/hearth-windows/og.svg' } },
        { tag: 'meta', attrs: { name: 'twitter:image', content: 'https://ericfinland.github.io/hearth-windows/og.svg' } },
        { tag: 'meta', attrs: { name: 'twitter:card', content: 'summary_large_image' } },
      ],
      sidebar: [
        {
          label: 'Getting Started',
          items: [
            { label: 'Overview', link: '/' },
            { label: 'Install and first run', link: '/getting-started/install/' },
            { label: 'The full guide', link: '/getting-started/guide/' },
          ],
        },
        {
          label: 'Concepts',
          items: [
            { label: 'The model shop', link: '/concepts/model-shop/' },
            { label: 'Signed updates', link: '/concepts/updates/' },
            { label: 'The agent swarm', link: '/concepts/agent-swarm/' },
            { label: 'MCP servers', link: '/concepts/mcp/' },
          ],
        },
        {
          label: 'Reference',
          items: [
            { label: 'Limitations', link: '/reference/limitations/' },
            { label: 'Threat model', link: '/reference/threat-model/' },
            { label: 'Privacy', link: '/reference/privacy/' },
            { label: 'Packaging and the installer', link: '/reference/packaging/' },
            { label: 'Licensing', link: '/reference/licensing/' },
            { label: 'Code signing policy', link: '/reference/code-signing/' },
          ],
        },
        {
          label: 'Project',
          items: [
            { label: 'About', link: '/project/about/' },
            { label: 'Contributing', link: '/project/contributing/' },
            { label: 'Security policy', link: '/project/security/' },
          ],
        },
      ],
    }),
  ],
});
