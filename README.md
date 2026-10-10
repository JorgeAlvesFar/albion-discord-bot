# Albion Discord Bot

Bot multi-servidor para Discord integrado à API real do Albion Online. Esta primeira entrega inclui configuração administrativa da guilda, tracker de combate com ativação explícita, isolamento por servidor Discord, repositórios em memória, cliente HTTP real e testes automatizados.

## Requisitos
- Python 3.12+
- Aplicação/bot criado no Discord Developer Portal
- Token do bot configurado em `.env`
- Permissões no Discord: View Channels, Send Messages, Embed Links, Use Application Commands, Manage Roles (se usar cargos futuramente), Manage Nicknames (se usar apelidos futuramente).

No Developer Portal, habilite **Server Members Intent** se for usar recursos de membros. O bot deve ter permissão para ver os canais escolhidos e enviar mensagens neles.

## Instalação local / NexusHost
1. Envie os arquivos do ZIP para a pasta do projeto no NexusHost.
2. Crie o ambiente Python, se o painel permitir:
   `python -m venv .venv`
3. Instale:
   `pip install -r requirements.txt`
4. Copie `.env.example` para `.env` e preencha `DISCORD_TOKEN`.
5. Execute:
   `python main.py`

Não compartilhe o `.env` nem publique o token. Se o painel tiver campo próprio para variáveis de ambiente, prefira usá-lo em vez de enviar o arquivo `.env`.

## Comandos
- `/ping`: valida que o bot está online.
- `/configurar`: abre a configuração do servidor Discord. É restrito a administradores.
- `/status_tracker`: mostra se o tracker está ativo e quais canais/tipos estão habilitados.

Em `/configurar`, pesquise a guilda pelo nome e selecione explicitamente um resultado. Depois, configure canais e opções de combate. O tracker só começa após ativação válida. A seleção de guilda não ativa o tracker.

## Limitações importantes desta entrega
- **Persistência em memória:** configurações, vínculos e histórico são perdidos ao reiniciar o processo. Não há PostgreSQL nesta versão.
- **API de combate:** a API comunitária do Albion não oferece aqui uma garantia formal de captura de 100%. A paginação, a janela de sobreposição e a deduplicação reduzem riscos, mas precisam ser observadas em operação real.
- **Sem entrega exatamente uma vez:** uma falha entre o envio ao Discord e a atualização do estado local pode causar uma publicação repetida após retentativa.
- **Configuração pós-reinício:** como não há persistência, configure novamente a guilda e o tracker depois de reiniciar.
- O cadastro completo de membros, recrutamento, sincronização automática de cargos/apelidos e opt-in PvE/PvP ainda não estão implementados nesta entrega; a arquitetura deixa esses módulos para as próximas etapas. Não considere esses recursos concluídos.
- A API real pode alterar contratos, limites ou formatos de resposta. Erros são registrados e não substituídos por dados inventados.
- **Card de kill/morte:** os ícones dos itens são consultados a cada evento em `render.albiononline.com`, sem cópia local. A prata é uma **estimativa** pelos preços de mercado do [Albion Online Data Project](https://www.albion-online-data.com/); itens sem oferta de venda entram como zero. Os rótulos do card são em inglês porque a fonte embutida no Pillow não tem acentos. Se a imagem falhar, a mensagem sai em texto.

## Testes
Instale as dependências e execute:
`pytest -q`

Os testes unitários usam mocks HTTP apenas dentro dos testes. O bot em execução sempre utiliza o cliente real da API do Albion.

## Estrutura
- `app/integrations/albion/client.py`: cliente HTTP real.
- `app/repositories/`: contratos e implementação em memória.
- `app/modules/configuration/`: interface de configuração Discord.
- `app/modules/combat_tracker/`: polling, paginação, deduplicação, card de kill/morte e publicação.
- `app/integrations/albion/prices.py`: estimativa de prata pelos preços de mercado.
- `app/assets/fame.png`: ícone de fama usado no card.
- `tests/`: testes unitários sem depender da disponibilidade da API.
