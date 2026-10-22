# Fluxos compartilhados da solvelab

## `ghcr-retention.yml` — só as 5 últimas versões de cada imagem

Cada publicação no GHCR deixa uma versão nova, e o armazenamento de Packages conta na cota da
organização. Em 2026-10-22 eram 25 imagens e ~4.900 versões, sem limpeza nenhuma (solvelab/.github#1).
Este fluxo roda logo depois do push e mantém as **5 versões com tag mais recentes** da imagem, junto
com os manifestos de plataforma e de atestação que elas usam. Apaga o resto.

Chamada, no `ci.yml` do repositório que publica a imagem, com `needs:` no job que faz o push:

```yaml
  retention:
    needs: publish
    if: github.event_name != 'pull_request'
    permissions:
      packages: write
    uses: solvelab/.github/.github/workflows/ghcr-retention.yml@main
    with:
      package: <nome-da-imagem>
```

`keep` muda a quantidade, `dry-run: true` só lista e `runner` troca o runner (padrão `k8s-runner`; quem roda o CI em `ubuntu-latest` passa o mesmo). `tag-pattern` é para o pacote que guarda mais de
uma família de tag: só as tags que casam entram na contagem, e as outras ficam intactas. O `octopus`
usa `^(v[0-9]|latest$)`, porque a imagem da sonda `octopus:probe-*` é fixada por digest no código.
O log mostra as tags mantidas e a contagem `<imagem>: N versões · mantém M (5 com tag) · apaga K`.
Uma falha na poda vira aviso e não derruba o CI, porque a imagem já foi publicada.

Poda avulsa, da máquina de quem mantém (token com `delete:packages`). Foi assim a primeira poda das 25
imagens, em 2026-10-22:

```bash
ORG=solvelab PACKAGE=<imagem> DRY_RUN=true GH_TOKEN=$(gh auth token) python3 scripts/ghcr-retention.py
```

### Garantias

- Os filhos de cada versão mantida são lidos no próprio registro, e nada é apagado antes de o conjunto
  a manter estar completo. Filho que existe no registro mas não aparece na API aborta sem apagar.
- Os filhos são lidos em qualquer nível (índice dentro de índice).
- Versão com tag cuja plataforma já foi apagada (o `docker pull` dela já não funciona) não conta entre
  as 5: ela é avisada e entra na poda. Se falta só a atestação, a imagem continua inteira.
- A versão com a tag `latest` nunca é apagada, mesmo depois de um rollback que mova a tag para uma
  versão antiga.
- Duas podas da mesma imagem não rodam ao mesmo tempo (`concurrency`).
- Versão sem tag criada há menos de 60 min (`GRACE_MIN`) nunca é apagada: pode ser filho de um push
  de outra execução que ainda não gravou a tag.

⚠️ Não trocar pela `actions/delete-package-versions`: ela conta cada manifesto filho como versão e
apaga a plataforma de imagens mantidas. Isso já aconteceu aqui: as tags antigas do
`fabcost3d-mqtt-agent` e do `fabcost3d-notification-hub` ficaram sem o manifesto da plataforma depois
de uma limpeza de versões sem tag. O fluxo foi portado do `DriveZoneFivem/.github`, que nasceu do mesmo
problema.

### Limite

Só dá para voltar até 5 imagens atrás. Se 5 publicações seguidas não chegarem ao cluster, a tag que
está rodando é apagada, e o pod não sobe de novo depois de reiniciar. O deploy de cada repositório
grava a tag nova no manifesto logo depois do push.

Testes do script: `python3 -m unittest discover -s tests` (rodam no CI deste repositório).
