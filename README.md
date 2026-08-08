# BraXYTDow 3.2

Aplicativo desktop para Windows 10/11 que baixa vídeos, áudios e playlists
públicas ou legitimamente acessíveis com fila, biblioteca inteligente, conversão, legendas, cortes e perfis de
qualidade. A interface não bloqueia durante análise, download ou pós-processamento.

> Use apenas conteúdo próprio, em domínio público ou para o qual você tenha
> autorização. Cookies opcionais apenas reutilizam uma sessão local legítima:
> eles não concedem direitos sobre obras, não removem DRM e não devem ser usados
> para violar direitos autorais ou controles de acesso.

## Ferramentas e atualização automática

O BraXYTDow executa `yt-dlp`, `ffmpeg`, `ffprobe` e, quando disponível, `deno`
como processos externos. A cópia empacotada é inicializada em uma pasta gravável
por usuário e funciona como fallback offline.

O atualizador interno pode verificar e instalar versões oficiais de forma
atômica, sem modificar o EXE instalado. yt-dlp usa o canal nightly por padrão,
pois tende a acompanhar mudanças do YouTube mais rapidamente; isso pode ser
alterado para stable nas configurações. Downloads de atualização são validados
por SHA-256 antes da ativação e uma instalação incompleta nunca substitui a ativa.

O **AutoCura 2.0** testa a cadeia YouTube → yt-dlp → Deno/EJS → FFmpeg antes de
promover uma versão, mantém duas cópias de recuperação, coloca releases
incompatíveis em quarentena e executa rollback depois de falhas repetidas. A
Central de Compatibilidade permite testar a cadeia e restaurar uma versão
anterior manualmente.

A verificação online usa vários vídeos-canários e separa falhas de rede, região,
conteúdo removido, limite temporário, EJS e PO Token. O EJS oficial remoto só é
habilitado sob falha local. Um provedor de PO Token pode ser instalado
opcionalmente: o pacote precisa estar na lista permitida, vir do repositório
esperado por HTTPS, possuir SHA-256 e passar por quarentena e teste funcional.
O plugin executa no processo externo do yt-dlp, nunca dentro da interface.

Outros recursos da versão 3.2:

- biblioteca permanente de IDs, sincronização de playlists somente com itens
  novos, busca e filtros por canal, formato, duração, data, status e arquivo;
- detecção de arquivos movidos/apagados, associação manual, download novamente
  e limpeza confirmada de parciais antigos e cópias duplicadas;
- capítulos selecionáveis, edição em lote, prioridade, fila reordenável,
  agendamento, limites por horário e pausa em conexão limitada;
- editor de nome, metadados e capa com finalização atômica pelo FFmpeg;
- bandeja do Windows, toast nativo com ações para abrir arquivo/pasta, modo
  compacto, texto ampliado, contraste reforçado e redução de movimento.
- créditos editoriais responsivos com retrato oficial de Richard Ittou, canais
  sociais destacados, identidade do criador e diagnóstico técnico recolhível.

### Sessão opcional com cookies

Em **Configurações > Sessão**, o usuário pode escolher um navegador compatível
ou um arquivo `cookies.txt` Mozilla/Netscape. A ativação exige consentimento
explícito de uso responsável. O BraXYTDow nunca solicita a senha e não copia o
conteúdo dos cookies para SQLite, fila, histórico, logs ou diagnóstico; somente
a origem selecionada é passada como argumento ao processo local do yt-dlp.

O arquivo precisa permanecer no disco local e em local protegido. Cookies
equivalem a uma sessão conectada: não os compartilhe, use apenas quando
necessário e evite volumes excessivos. O projeto yt-dlp alerta que contas podem
sofrer bloqueio temporário ou permanente. Se um provedor PO Token também estiver
ativo, ele será carregado no mesmo processo externo do yt-dlp.

Não existe garantia de compatibilidade permanente com serviços de terceiros.
Quando o YouTube mudar, use **Ferramentas > Verificar atualizações** e consulte o
diagnóstico do aplicativo.

## Executar em desenvolvimento

Requisitos: Windows x64, Python 3.11 ou mais recente e PowerShell 5.1+.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
powershell -ExecutionPolicy Bypass -File scripts\prepare_binaries.ps1
python -m baixatube
```

`prepare_binaries.ps1` preserva binários válidos existentes. Quando precisa
baixar, usa assets oficiais e confere os checksums publicados de yt-dlp, FFmpeg
e Deno antes de substituir qualquer arquivo. Opções úteis:

```powershell
# Validar e reutilizar tudo localmente, sem rede
powershell -File scripts\prepare_binaries.ps1 -Offline

# Atualizar explicitamente todas as ferramentas empacotadas
powershell -File scripts\prepare_binaries.ps1 -Force

# Build intencionalmente sem Deno
powershell -File scripts\prepare_binaries.ps1 -SkipDeno
```

## Testes

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Os testes usam respostas locais e bancos temporários; não dependem do YouTube.

## Criar a release Windows

O build padrão usa primeiro `.venv`, valida ferramentas, executa os testes e
gera as variantes onefile e onedir. Dependências de build exatas estão em
`requirements-build-lock.txt`.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build.ps1
```

Artefatos:

- `dist\BraXYTDow.exe`: EXE único, sem Python/FFmpeg previamente instalados;
- `dist\BraXYTDow-3.2.0-windows-x64.zip`: pacote portátil com inicialização mais rápida;
- `dist\installer\BraXYTDow-Setup-3.2.0-x64.exe`: instalador por usuário, quando Inno Setup 6 estiver instalado;
- `dist\BraXYTDow-sbom.cdx.json`: inventário CycloneDX das dependências e ferramentas;
- `dist\SHA256SUMS.txt` e `dist\release-manifest.json`: integridade e proveniência da release.

O EXE único extrai internamente o runtime na inicialização, comportamento normal
do PyInstaller onefile. Para abertura mais rápida e menor uso temporário de
disco, prefira o ZIP ou o instalador.

Opções de build importantes:

```powershell
# Build totalmente offline com a venv e binários já preparados
powershell -File scripts\build.ps1 -Offline

# Sincronizar a venv com o lock e atualizar ferramentas antes do build
powershell -File scripts\build.ps1 -SyncDependencies -RefreshTools

# Exigir que o instalador também seja produzido
powershell -File scripts\build.ps1 -RequireInstaller

# Release oficial: exigir certificado Authenticode disponível no repositório do usuário
powershell -File scripts\build.ps1 -RequireInstaller -RequireSigning `
  -CertificateThumbprint "SEU_THUMBPRINT_DE_40_CARACTERES"
```

O script falha se a versão do runtime, a versão do empacotamento, os binários ou
o conteúdo interno da release forem inconsistentes. `scripts\verify_release.ps1`
também pode ser executado separadamente após o PyInstaller.

### Publicar atualização do próprio aplicativo

O workflow `.github/workflows/release.yml` executa testes, build Windows,
assinatura, SBOM, publicação no GitHub Releases e atestação de procedência. A
publicação falha de forma segura se EXE ou instalador não tiverem Authenticode
confiável e carimbo de tempo. O workflow aceita Microsoft Artifact Signing com
OIDC ou certificado público PFX quando a autoridade certificadora permitir esse
formato. A configuração completa e os nomes dos secrets estão em
[`DISTRIBUTION.md`](DISTRIBUTION.md).

Depois de assinar e hospedar o instalador em HTTPS, gere ou atualize o manifesto
de canais:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\publish_app_manifest.ps1 `
  -InstallerUrl "https://seu-dominio/BraXYTDow-Setup-3.2.0-x64.exe" `
  -Channel stable -Rollout 10 `
  -Notes "BraXYTDow 3.2"
```

Publique também `dist\latest.json` em HTTPS. O aplicativo exige schema 2,
canal estável ou experimental, rollout determinístico, tamanho exato, SHA-256,
assinatura Authenticode, identidade do publicador e certificado fixado. Ao
atualizar, ele mantém um instalador anterior verificado e reverte depois de três
inicializações malsucedidas. A instalação só começa após confirmação.

## Dados locais e privacidade

Configurações, fila, histórico, logs e ferramentas atualizadas ficam sob
`%LOCALAPPDATA%\BraXYTDow`. Atualizações de uma instalação existente continuam
usando `%LOCALAPPDATA%\BaixaTube` para preservar a fila, o histórico e as ferramentas.
Cookies ficam desativados por padrão. Quando o usuário os ativa, somente a
origem escolhida (navegador/perfil ou caminho do arquivo) é persistida para
recuperar a fila; o conteúdo secreto não é copiado. Os endereços informados são
enviados somente aos processos externos necessários para analisar e baixar a
mídia solicitada.

## Créditos

BraXYTDow foi criado e desenvolvido por **Richard Ittou**.

- Instagram: [@Richard.ittou](https://www.instagram.com/richard.ittou?igsh=c210bHhzdzJwcWg1)
- Discord: `richardxlrr`

## Licenças de terceiros

Consulte [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md). O manifesto
`bin\tools-manifest.json`, gerado durante o build, registra versão, origem,
tamanho e SHA-256 exatos das ferramentas incorporadas.
