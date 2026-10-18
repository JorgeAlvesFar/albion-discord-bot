# Guia de uso do bot

Este guia explica, de forma simples, o que cada comando faz e como usá-lo. Para digitar um comando, escreva `/` no chat do Discord e escolha o comando na lista.

O bot tem quatro comandos:

| Comando | Quem usa | Para quê |
| --- | --- | --- |
| `/cadastrar` | Administradores | Dizer ao bot qual é a guilda do Albion deste servidor e onde publicar a verificação de membros. |
| `/registrar-membros` | Administradores | Escolher o cargo e a tag que os membros registrados recebem. |
| `/registrar` | Todos os membros | Vincular o seu personagem do Albion à sua conta do Discord. |
| `/killboard` | Administradores | Publicar no Discord as kills e as mortes da guilda. |

## Por onde começar

**Administradores**, nesta ordem:
1. `/cadastrar`: escolha a guilda e, se quiser, o canal e o horário da verificação de membros. Sem a guilda, os outros comandos não funcionam.
2. `/registrar-membros`: opcional. Escolha um cargo e/ou uma tag para os membros.
3. `/killboard`: opcional. Escolha os canais e ligue as notificações.
4. Avise os membros para usarem `/registrar`.

**Membros**: use `/registrar` e digite o nome do seu personagem.

### Coisas que valem para todos os comandos
- As telas de configuração aparecem **só para você** (o Discord mostra "Só você pode ver esta mensagem").
- Quando um administrador termina uma alteração, o bot publica no canal um **resumo** do que foi feito e por quem.
- Se você deixar uma tela aberta por muito tempo, ela **expira**. É só usar o comando de novo.
- **Importante:** quando o bot é reiniciado (por exemplo, numa atualização), ele esquece as configurações e os registros. Os administradores precisam configurar de novo, e os membros precisam usar `/registrar` de novo. Cargos e apelidos que já estavam no Discord continuam lá.

---

## `/cadastrar`: escolher a guilda (administradores)

Liga este servidor do Discord a uma guilda do Albion Online. Tudo o mais depende disso. No mesmo painel você escolhe **onde** o bot avisa quando alguém sai da guilda e **a que horas** sai o relatório diário.

**Como usar:**
1. Digite `/cadastrar`. Aparece a guilda atual (se já houver uma), o canal da verificação e o horário do relatório.
2. Clique em **Pesquisar guilda** e digite o nome da guilda.
3. Escolha a guilda certa na lista. Confira o **Guild ID**: guildas com nomes parecidos são diferentes (por exemplo, "TELETOM" e "T E L E T O M").
4. Clique em **Confirmar guilda**.
5. Escolha o **canal da verificação de membros** (canal de texto) e o **horário do relatório diário** (fuso de Brasília). Clique em **Salvar verificação**.

**O que o bot publica nesse canal:**
- **Na hora:** uma mensagem cada vez que a verificação remove o cargo de alguém que não está mais na guilda. O registro dessa pessoa é desativado; para voltar, ela usa `/registrar` de novo.
- **Uma vez ao dia**, no horário escolhido: um relatório com o que foi verificado desde a última postagem (quantos registros foram checados, quem foi removido e se a API falhou em alguma consulta).

Se você não escolher um canal, a verificação continua acontecendo (quem sai perde o cargo), mas **nada é publicado**.

**Bom saber:**
- O bot nunca escolhe a guilda sozinho; você sempre confirma.
- **Trocar a guilda desliga o killboard** e apaga os canais escolhidos nele. O canal e o horário da verificação de membros **permanecem**. Configure o `/killboard` de novo depois.
- Ao trocar a guilda, os membros precisam usar `/registrar` de novo com personagens da nova guilda.
- **Redefinir** remove a guilda (e, junto, o killboard) e também apaga o canal e o horário da verificação. O bot pede confirmação antes.

---

## `/registrar-membros`: cargo e tag dos membros (administradores)

Define o que acontece com quem se registra. As duas opções são **opcionais**.

- **Cargo:** o cargo do Discord que o membro registrado recebe. Se não escolher nenhum, ninguém recebe cargo.
- **Tag no apelido:** um texto curto (até 13 letras) que vai na frente do nome do personagem. Exemplo: com a tag `TLT`, o apelido fica `[TLT] LeonamUchiha`. Sem tag, o bot não mexe no apelido.

**Como usar:**
1. Digite `/registrar-membros`.
2. Escolha o cargo no menu (ou deixe vazio).
3. Clique em **Editar tag** para escrever a tag (ou deixe vazia).
4. Clique em **Salvar**.

**Se o bot recusar o cargo**, normalmente é por um destes motivos:
- O cargo está **acima** do cargo do bot. Vá em Configurações do servidor → Cargos e arraste o cargo do bot para cima do cargo escolhido.
- O bot não tem a permissão **Gerenciar cargos** (ou **Gerenciar apelidos**, para a tag).

**Bom saber:**
- As mudanças valem para os **próximos** registros. Quem já se registrou continua com o que tinha.
- **Redefinir** apaga o cargo e a tag desta configuração (pede confirmação). Não mexe na guilda nem no killboard.

---

## `/registrar`: vincular o seu personagem (todos os membros)

Liga o seu personagem do Albion à sua conta do Discord. Só funciona se o personagem estiver **na guilda deste servidor**.

**Como usar:**
1. Digite `/registrar`.
2. Escreva o nome do seu personagem **exatamente como aparece no jogo** e envie.
3. Se aparecer mais de um personagem parecido, escolha o seu na lista.
4. Pronto: o bot mostra o resultado (só para você).

Se o servidor tiver cargo e tag configurados, você recebe o cargo e o seu apelido muda para `[TAG] SeuPersonagem`.

**Se não der certo:**
- *"Nenhum personagem encontrado"*: confira o nome, letra por letra.
- *"está atualmente na guilda ..." ou "sem guilda"*: o personagem não está na guilda do servidor. Se você acabou de entrar na guilda, a API do Albion pode demorar um pouco para atualizar; tente mais tarde.
- *"já está vinculado a outro membro"*: outra pessoa do servidor registrou esse personagem. Fale com um administrador.
- *"A API do Albion não respondeu"*: o servidor do jogo não respondeu. Nada foi alterado; tente de novo em instantes.
- *"O registro está válido, mas..."*: você foi registrado, mas o bot não conseguiu dar o cargo ou trocar o apelido (por exemplo, o dono do servidor não pode ter o apelido alterado por bots). Avise um administrador.

**Bom saber:**
- Cada pessoa tem **um** personagem registrado por servidor. Para trocar de personagem, use `/registrar` de novo: o bot pergunta se você quer substituir.
- O bot confere de tempos em tempos (a cada 15 minutos, por padrão) se o seu personagem ainda está na guilda. Se não estiver mais, por qualquer motivo (saiu, foi expulso, mudou de guilda, apagou o personagem), você **perde o cargo** do servidor. O apelido não muda. Se os administradores tiverem configurado um canal em `/cadastrar`, o bot avisa lá na hora.
- Depois disso o seu registro fica **desativado**. Se você voltar para a guilda, **use `/registrar` de novo** para receber o cargo outra vez; o bot não devolve sozinho.

---

## `/killboard`: kills e mortes no Discord (administradores)

Faz o bot publicar automaticamente, com uma imagem do combate, as **kills** (quando alguém da guilda mata) e as **mortes** (quando alguém da guilda morre).

**Como usar:**
1. Digite `/killboard`. (É preciso ter feito o `/cadastrar` antes.)
2. Escolha o **canal de kills** e/ou o **canal de mortes**. Pode ser o mesmo canal para os dois.
3. Use os botões **Kills** e **Mortes** para ligar ou desligar cada tipo.
4. Clique em **Salvar**.

**Para desligar:** desligue os dois tipos e clique em **Salvar**. Os canais ficam guardados para quando quiser religar.

**Bom saber:**
- O bot só publica combates que acontecerem **depois** que você salvar. Combates antigos não aparecem.
- **A API do Albion pode atrasar** (já chegou a horas de atraso). Se nada aparecer logo depois de ativar, é provável que a API ainda não tenha liberado os combates; eles aparecem quando ela alcançar.
- Cada vez que você salva uma alteração com o killboard ligado, a contagem recomeça a partir daquele momento.
- O bot precisa poder **ver o canal, enviar mensagens, incorporar links e anexar arquivos** nos canais escolhidos. Se faltar alguma permissão, ele avisa ao salvar.
- O valor em prata mostrado na imagem é uma **estimativa** pelos preços de mercado.
- **Redefinir** desliga o killboard e apaga os canais escolhidos (pede confirmação). Não mexe na guilda nem nos membros.
