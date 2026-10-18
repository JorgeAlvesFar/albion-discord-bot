# Albion Discord Bot

Bot para Discord integrado à API real do Albion Online. Um único bot (um único token) atende vários servidores Discord, cada um associado à sua guilda do Albion, com configurações, membros e notificações isolados por servidor.

Funcionalidades:
- **`/cadastrar`**: associa o servidor a uma guilda do Albion.
- **`/registrar-membros`**: define o cargo e a tag no apelido dos membros registrados.
- **`/registrar`**: cada membro vincula o próprio personagem, validado na guilda.
- **`/killboard`**: publica kills e mortes da guilda, com card de equipamentos.

Guia para quem vai usar o bot (administradores e membros): [`docs/GUIA_DE_USO.md`](docs/GUIA_DE_USO.md).

## Requisitos
- Python 3.12+
- Aplicação/bot criado no Discord Developer Portal, com **Server Members Intent** habilitado.
- Token do bot em `.env` ou nas variáveis de ambiente do painel de hospedagem.

### Permissões do bot no Discord
| Permissão | Para quê |
| --- | --- |
| Use Application Commands | Comandos de barra. |
| View Channels, Send Messages, Embed Links | Painéis, resumos e notificações. |
| Attach Files | Card de kill/morte nos canais do killboard. |
| Manage Roles | Atribuir o cargo configurado em `/registrar-membros`. O cargo do bot precisa ficar **acima** desse cargo em Configurações do servidor → Cargos. |
| Manage Nicknames | Aplicar a tag no apelido. O bot não consegue alterar o apelido do dono do servidor nem de quem tem cargo acima do dele. |

## Instalação e atualização (local ou NexusHost)
1. Envie os arquivos para a pasta do projeto (ou faça o deploy pelo repositório).
2. Crie o ambiente Python, se o painel permitir: `python -m venv .venv`
3. Instale as dependências: `pip install -r requirements.txt`
4. Configure as variáveis de ambiente (copie `.env.example` para `.env` e preencha `DISCORD_TOKEN`, ou use o campo de variáveis do painel).
5. Comando de inicialização: `python main.py`

Para atualizar: substitua os arquivos (ou faça `git pull`), rode `pip install -r requirements.txt` de novo e reinicie o bot. **Ao reiniciar, tudo o que está em memória se perde** (veja Limitações): refaça `/cadastrar`, `/registrar-membros` e `/killboard`, e peça aos membros para usar `/registrar` de novo.

Na inicialização, o bot sincroniza os comandos com o Discord e registra no log quais foram sincronizados. Comandos removidos (como o antigo `/configurar`) podem continuar aparecendo por alguns minutos no aplicativo do Discord.

Não compartilhe o `.env` nem publique o token.

## Variáveis de ambiente
| Variável | Padrão | Uso |
| --- | --- | --- |
| `DISCORD_TOKEN` | — | Token do bot (obrigatório). |
| `DATA_STORE_MODE` | `memory` | Único modo suportado nesta versão. |
| `ALBION_API_BASE_URL` | `https://gameinfo.albiononline.com/api/gameinfo` | Servidor do jogo (Américas). Europa: `https://gameinfo-ams.albiononline.com/api/gameinfo`; Ásia: `https://gameinfo-sgp.albiononline.com/api/gameinfo`. Define também a região dos preços de mercado. |
| `ALBION_HTTP_TIMEOUT` | `15` | Timeout das consultas HTTP, em segundos. |
| `ALBION_MAX_CONCURRENCY` | `4` | Consultas simultâneas à API do Albion. |
| `ALBION_RETRY_ATTEMPTS` | `3` | Tentativas por consulta à API do Albion. |
| `COMBAT_POLL_SECONDS` | `45` | Intervalo **máximo** entre leituras do feed de combate (15 a 120 s). Com mais atividade, o coletor lê com mais frequência, até a cada 10 s. |
| `MEMBER_SYNC_SECONDS` | `900` | Intervalo da verificação de quem saiu ou voltou para a guilda (300 a 3600 s). |
| `LOG_LEVEL` | `INFO` | Nível de log. `DEBUG` mostra o motivo de cada evento descartado. |

## Comandos
Os comandos administrativos só aparecem, por padrão, para quem tem a permissão **Administrador**, e o bot confere essa permissão a cada clique. As etapas intermediárias são privadas; ao concluir uma operação que muda algo, o bot publica um resumo no canal do comando com a menção de quem a realizou (a menção não notifica ninguém). Painéis expiram depois de alguns minutos sem uso e avisam que expiraram; um segundo clique numa operação já concluída não a repete.

### `/cadastrar` (administradores)
Associa o servidor a uma guilda do Albion.
- O painel mostra a guilda atual (nome, aliança e Guild ID) e o estado do killboard. Botões: **Redefinir** (esquerda) e **Pesquisar guilda** (direita).
- **Pesquisar guilda**: busca pelo nome na API real e lista os resultados com nome, aliança e Guild ID. A guilda nunca é escolhida automaticamente: é preciso selecionar e confirmar. Nomes parecidos podem ser guildas diferentes (ex.: "TELETOM" e "T E L E T O M").
- Trocar a guilda **desativa e apaga a configuração do killboard** e descarta as notificações pendentes. Os vínculos de personagens não são transferidos. Confirmar a mesma guilda não muda nada.
- **Redefinir** pede confirmação e remove a guilda e, junto, a configuração do killboard (que depende dela). A configuração de membros é mantida.
- Se a API do Albion falhar, a guilda já cadastrada é mantida.

### `/registrar-membros` (administradores)
Configura duas opções independentes e opcionais aplicadas a quem se registrar com `/registrar`.
- **Cargo**: escolhido no menu nativo de cargos do Discord. Vazio = nenhum cargo é atribuído. O bot recusa @everyone, cargos gerenciados por integrações e cargos acima ou na mesma posição do cargo mais alto do bot, e exige **Gerenciar cargos**.
- **Tag no apelido** (botão **Editar tag**): até 13 caracteres, aplicada como `[TAG] NomeDoPersonagem`. Vazia = o apelido não é alterado. Exige **Gerenciar apelidos**.
- **Redefinir** (esquerda, pede confirmação) apaga só o cargo e a tag; **Salvar** (direita) grava e publica o resumo. Valem para os próximos registros; cargos e apelidos já aplicados não são alterados. A guilda e o killboard não mudam.

### `/registrar` (todos os membros)
Vincula o personagem do Albion à conta Discord. Todas as respostas são privadas.
- Exige a guilda cadastrada. Pede o nome do personagem numa janela privada e pesquisa na API real. Com o nome exato, segue direto; com resultados parecidos, pede para escolher.
- Confirma pelo **perfil atual** do personagem que ele está na guilda cadastrada. Se a API não responder, nada é alterado.
- Um personagem só pode estar vinculado a um membro por servidor; cada membro tem um único personagem (registrar outro pede confirmação para substituir).
- Aplica o cargo e a tag configurados, se houver. Falhas ao aplicar o cargo ou o apelido são informadas, mas o vínculo continua válido.

**Verificação periódica** (a cada `MEMBER_SYNC_SECONDS`): quem saiu da guilda perde o cargo configurado, mantendo o vínculo e o apelido; quem voltou recupera o cargo. Só muda algo com resposta válida da API: a ausência na lista de membros é confirmada pelo perfil do personagem, e falhas ou listas vazias não removem cargos. Também aplica o cargo atual a membros registrados antes de uma troca de cargo (o cargo antigo não é removido).

### `/killboard` (administradores)
Configura as notificações de combate. Exige a guilda cadastrada e é independente da configuração de membros.
- O painel mostra o estado (ativo desde quando, ou desativado), um seletor de canal para kills e outro para mortes e um botão para ligar/desligar cada tipo. Kills e mortes são independentes e podem usar o mesmo canal. Tudo começa desligado. Botões: **Redefinir**, **Kills**, **Mortes**, **Salvar**.
- **Salvar** valida os tipos ligados: canal escolhido, existente e de texto, e permissões do bot para ver o canal, enviar mensagens, incorporar links e anexar arquivos. Com erro, nada é salvo e o painel continua aberto.
- Com algum tipo ligado, o killboard fica ativo e **toda gravação define um novo instante de referência**: só combates a partir dali são publicados e as notificações pendentes da configuração anterior são descartadas. Com os dois tipos desligados, o killboard fica desativado e os canais ficam guardados para religar.
- **Redefinir** (pede confirmação) desativa o killboard, apaga os canais e descarta as pendências. A guilda e a configuração de membros não mudam.

## Como o tracker de combate funciona
- **Coletor**: lê o feed global de eventos (`/events`) do topo até a página que se sobrepõe à leitura anterior, deduplicando pelo `EventId`. A primeira leitura ao ligar só marca os eventos como vistos. O intervalo se adapta ao volume de eventos novos (mira em até ~300 por leitura), entre 10 s e `COMBAT_POLL_SECONDS`, e cai para o mínimo quando uma leitura atinge o limite de paginação da API. O feed é lido uma vez por ciclo para todos os servidores; cada servidor filtra a própria guilda como killer (kill) ou vítima (morte).
- **Filtro temporal**: só entram eventos cujo **horário do combate** é igual ou posterior à ativação do killboard daquele servidor. Um combate anterior que chega atrasado ao feed é descartado.
- **Fila e publicador**: cada evento relevante vira uma notificação por tipo (kill e/ou morte), com estado próprio, numa fila em memória. O publicador envia até 4 notificações em paralelo, sem bloquear o coletor. Falhas temporárias do Discord (5xx, 429, rede) são refeitas após 15 s, 30 s, 1 min, 2 min e 5 min (até 6 tentativas); se só uma das notificações de um evento falhar, só ela é refeita. Falhas permanentes (canal apagado, sem permissão, outros 4xx) encerram a notificação com log do motivo.
- **Versão da configuração**: salvar ou redefinir o killboard, trocar ou redefinir a guilda incrementa a versão; notificações criadas sob uma versão anterior são descartadas. A configuração é revalidada antes de cada envio, inclusive depois de gerar o card.
- **Card**: imagem com o equipamento do killer e da vítima, o inventário da vítima, fama, valor estimado em prata e participantes. Ícones vêm de `render.albiononline.com` a cada evento (sem cópia local); a prata é uma **estimativa** pelos preços do [Albion Online Data Project](https://www.albion-online-data.com/). Se a imagem falhar ou não ficar pronta em 20 s (o serviço de ícones já levou mais de 40 s por card), a mensagem sai em texto, para não atrasar a fila.
- **Logs**: cada ciclo registra duração, páginas, eventos recebidos/novos/duplicados/inválidos, erros da API, atraso da API dos eventos novos, descartes por motivo (com um exemplo), tamanho da fila, publicações com sucesso e falha, e o intervalo escolhido com a justificativa. Cada publicação registra o horário do combate, o horário em que o coletor viu o evento e o horário da publicação, com o atraso da API, o atraso do bot e o total.

### Diagnóstico do tracker
Os problemas encontrados e corrigidos nesta versão:

| Problema | Causa | Correção |
| --- | --- | --- |
| Kills/mortes não publicadas | Os endpoints filtrados por guilda (`events?guildId=`, `players/{id}/deaths`) chegam a ficar horas atrasados em relação ao feed global. | Coleta pelo feed global, filtrando a guilda localmente. |
| Combates anteriores à ativação publicados | O filtro por horário de ativação deixou de existir na troca para o feed global. | Filtro pelo horário do combate, aplicado ao enfileirar e revalidado antes de enviar. |
| Falha de publicação nunca refeita | O evento já tinha sido visto no feed e não voltava. | Fila em memória com novas tentativas e estado por notificação. |
| Publicação lenta atrasava a coleta | Coleta e publicação no mesmo laço. | Coletor e publicador em tarefas separadas; publicações em paralelo. |
| Pendências após desativar/reconfigurar | Não havia como saber a qual configuração uma pendência pertencia. | Versão da configuração, conferida antes de cada envio. |

Observado na API real: o feed global é atualizado em lotes (ciclos sem nada novo seguidos de 70 a 240 eventos) e esteve com **4 minutos** e com **mais de 6 horas** de atraso em dias diferentes. Esse atraso é da API, não do bot, e aparece no log de cada ciclo.

## Limitações conhecidas
- **Armazenamento em memória**: configurações, vínculos de personagens, notificações pendentes e o histórico de deduplicação se perdem ao reiniciar. Cargos e apelidos já aplicados no Discord permanecem. A migração para banco de dados é um escopo futuro.
- **Atraso da API**: como só são publicados combates posteriores à ativação, com a API atrasada horas nada sai logo após ativar. Quem acabou de entrar ou sair da guilda também pode demorar a aparecer assim na API.
- **Limite de paginação**: a API só permite ler ~1.020 eventos para trás por leitura (offset máximo 1000). Se entre duas leituras entrarem mais eventos que isso (bot parado, API fora do ar ou batalhas enormes), os mais antigos se perdem; o log avisa.
- **Sem entrega exatamente uma vez**: se o Discord aceitar a mensagem e o bot falhar antes de registrar o envio, uma nova tentativa pode gerar duplicata.
- **Captura não garantida**: a API do Albion não oferece garantia formal de captura de 100%; a paginação, a sobreposição e a deduplicação reduzem riscos, mas precisam ser observadas em operação.
- **Retenção**: eventos concluídos saem da memória; só as últimas 20.000 chaves ficam para deduplicação.
- **Pesquisa de guilda**: a API costuma devolver a aliança vazia; o Guild ID é o que diferencia guildas de nomes parecidos.
- **Textos do card** evitam acentos porque a fonte embutida no Pillow não os tem.
- Não há comando para desfazer um vínculo de personagem. Recrutamento, formulários de fama PvE/PvP e disponibilidade para ZvZ não fazem parte desta versão.

## Testes
Instale as dependências e execute `pytest -q`. Os testes usam API e Discord simulados; o bot em execução sempre usa a API real.

Resultado nesta versão: **150 testes passando**, estáveis em execuções repetidas. Incluem testes de regressão dos defeitos do tracker (confirmados por mutação: reintroduzir cada defeito faz a suíte falhar) e testes integrados com os quatro módulos, dois servidores, falhas do Discord e da API, volumes acima do limite de paginação e reinício.

Validações feitas contra a API real (fora dos testes automatizados): coleta e publicação de eventos reais com card e medição de latência; filtro temporal descartando combates anteriores à ativação; pesquisa de guildas e personagens; confirmação de guilda pelo perfil; verificação periódica sem remoção indevida de cargo.

Pendentes de validação no Discord real (dependem do deploy): uso dos comandos num servidor, aplicação de cargo e apelido com a hierarquia real e publicação de um combate real da guilda depois da ativação.

## Estrutura
- `main.py`: ponto de entrada (carrega o `.env` e inicia o bot).
- `app/main.py`: montagem do bot, comandos e tarefas.
- `app/core/`: configuração e logs (em UTC).
- `app/integrations/albion/`: cliente da API do Albion (`client.py`), preços de mercado (`prices.py`) e região (`regions.py`).
- `app/repositories/models.py`: modelos. A configuração de cada servidor (`ServerConfig`) tem uma parte por módulo: `GuildLink` (`/cadastrar`), `MembersConfig` (`/registrar-membros`) e `KillboardConfig` (`/killboard`); cada módulo só altera a própria parte.
- `app/repositories/`: armazenamento em memória, um por assunto: `config_store.py` (configurações), `link_store.py` (vínculos de personagens, com exclusividade), `event_store.py` (fila de notificações, deduplicação e retenção). `memory.py` junta os três em `MemoryRepositories`.
- `app/modules/common.py`: regras comuns dos comandos (permissão, painéis privados, resumo público).
- `app/modules/guild_registration/`: `/cadastrar` (regras em `service.py`, interface em `cog.py`).
- `app/modules/members_config/`: `/registrar-membros` (regras de tag, apelido e cargo em `service.py`, interface em `cog.py`).
- `app/modules/member_registration/`: `/registrar` e a verificação periódica de membros.
- `app/modules/combat_tracker/`:
  - `events.py`: leitura dos eventos da API (horário em UTC, guilda dos envolvidos, itens).
  - `feed.py`: leitura do feed global com sobreposição e deduplicação.
  - `service.py`: `CombatTracker`, que coordena o coletor (feed → fila), o filtro por servidor e o intervalo adaptativo.
  - `publisher.py`: publicador (fila → Discord), com revalidação, novas tentativas e falhas permanentes.
  - `messages.py`: montagem da mensagem (card ou texto).
  - `card.py`: imagem do combate.
  - `killboard.py` e `cog.py`: regras e interface do `/killboard`.
- `app/assets/fame.png`: ícone de fama usado no card.
- `docs/GUIA_DE_USO.md`: guia de uso dos comandos.
- `tests/`: testes automatizados.
