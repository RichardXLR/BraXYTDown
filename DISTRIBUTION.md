# Distribuição oficial e assinatura do BraXYTDow

Uma release pública do BraXYTDow só é considerada oficial quando o EXE e o
instalador passam por todas estas verificações:

- assinatura Authenticode válida e confiável no Windows;
- carimbo de tempo RFC 3161 com SHA-256;
- identidade do publicador registrada no relatório de assinatura;
- tamanho e SHA-256 publicados em `SHA256SUMS.txt` e `release-manifest.json`;
- SBOM CycloneDX e atestação de procedência do GitHub Actions;
- teste funcional do executável empacotado antes da publicação.

O workflow `.github/workflows/release.yml` falha antes de publicar se qualquer
uma dessas condições não for atendida. Certificados autoassinados não devem ser
usados em releases públicas: eles servem somente para laboratório e continuam
aparecendo como não confiáveis em outros computadores.

## Escolher o provedor de assinatura

### Opção A — Microsoft Artifact Signing

Use esta opção quando a conta e o perfil de certificado estiverem disponíveis
para a identidade e a região do publicador. É o caminho preferencial do workflow
porque a chave privada não é exportada nem armazenada no GitHub.

1. No Azure, crie uma conta do Artifact Signing, conclua a validação de
   identidade e crie um perfil de certificado de confiança pública.
2. Dê à identidade do GitHub a função **Artifact Signing Certificate Profile
   Signer** apenas no perfil usado pelo BraXYTDow.
3. Configure uma credencial federada OIDC para o repositório e para o ambiente
   GitHub `production-release`.
4. Cadastre estes secrets no ambiente `production-release`:

   - `AZURE_CLIENT_ID`
   - `AZURE_TENANT_ID`
   - `AZURE_SUBSCRIPTION_ID`

5. Cadastre estas variables no mesmo ambiente:

   - `BRAXY_ARTIFACT_SIGNING_ENDPOINT` — por exemplo,
     `https://brs.codesigning.azure.net/` quando a conta estiver em Brazil South;
   - `BRAXY_ARTIFACT_SIGNING_ACCOUNT`
   - `BRAXY_ARTIFACT_SIGNING_PROFILE`

6. Execute o workflow **BraXYTDow signed release** e selecione
   `artifact-signing`.

O workflow autentica no Azure por OIDC, assina os dois executáveis do aplicativo,
recria o ZIP e o instalador a partir dos binários assinados, assina o instalador
e valida novamente o pacote completo.

Se o **Artifact Signing Client Tools** estiver instalado nesta máquina, também
é possível executar a assinatura local. Primeiro autentique uma identidade que
tenha a função de assinante do perfil; depois feche o BraXYTDow e execute:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  scripts\build_artifact_signed_release.ps1 `
  -Endpoint "https://REGIAO.codesigning.azure.net/" `
  -CodeSigningAccountName "NOME_DA_CONTA" `
  -CertificateProfileName "NOME_DO_PERFIL" `
  -SyncDependencies
```

O script usa a DLL instalada pelo pacote, cria os metadados somente em arquivo
temporário, assina com o timestamp oficial da Microsoft, verifica os arquivos,
recria o instalador e gera `signing-report.json`. Tokens e chaves não são
gravados no projeto.

Documentação oficial:

- <https://learn.microsoft.com/azure/artifact-signing/how-to-signing-integrations>
- <https://github.com/Azure/artifact-signing-action>

### Opção B — certificado de uma autoridade certificadora

Use um certificado público de **Code Signing** emitido para Richard Ittou ou
para a pessoa jurídica responsável pelo aplicativo. Siga o método de custódia
da chave oferecido pela autoridade certificadora. Se o certificado for
exportável em PFX, o workflow também aceita esse formato.

Para build local, instale o Windows SDK e o certificado no repositório pessoal
do Windows. Depois liste o thumbprint e produza a release:

```powershell
Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert |
  Select-Object Subject, Thumbprint, NotAfter, HasPrivateKey

powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File scripts\build.ps1 `
  -RequireInstaller -RequireSigning `
  -CertificateThumbprint "THUMBPRINT_SHA1_DE_40_CARACTERES"
```

Para GitHub Actions com PFX, cadastre no ambiente `production-release`:

- `BRAXY_SIGNING_PFX_BASE64` — conteúdo binário do PFX convertido em Base64;
- `BRAXY_SIGNING_PASSWORD` — senha forte e exclusiva do PFX.

O PFX é gravado somente no diretório temporário efêmero do runner, importado no
repositório do usuário e apagado ao final do job. Nunca adicione PFX, PEM, P12 ou
chaves privadas ao projeto. Esses formatos estão bloqueados pelo `.gitignore`.

Documentação oficial do SignTool:

- <https://learn.microsoft.com/windows/win32/seccrypto/signtool>

## Publicar a release

O projeto precisa estar em um repositório GitHub antes da primeira publicação.
Crie o ambiente `production-release`, aplique proteção de aprovação para o
criador e configure somente um dos provedores acima. Depois:

1. abra **Actions → BraXYTDow signed release → Run workflow**;
2. escolha o provedor, canal `stable` ou `experimental`, rollout e notas;
3. aguarde build, testes, assinatura, verificação e atestação;
4. baixe o instalador da Release criada pelo workflow;
5. confirme o relatório `signing-report.json` e os hashes antes de divulgar.

Artefatos oficiais esperados:

- `BraXYTDow-Setup-3.2.0-x64.exe` — instalador recomendado;
- `BraXYTDow.exe` — executável portátil em arquivo único;
- `BraXYTDow-3.2.0-windows-x64.zip` — pacote portátil de inicialização rápida;
- `signing-report.json` — publicador, certificado, timestamp e hashes;
- `release-manifest.json`, `SHA256SUMS.txt` e `BraXYTDow-sbom.cdx.json`.

Para conferir arquivos assinados localmente:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command `
  "& '.\scripts\assert_authenticode.ps1' `
    -Path @('.\dist\BraXYTDow.exe', '.\dist\installer\BraXYTDow-Setup-3.2.0-x64.exe') `
    -RequireTimestamp -RequireSignTool"
```

Uma assinatura válida comprova integridade e publicador, mas o Microsoft
SmartScreen ainda pode mostrar aviso enquanto a nova identidade constrói
reputação de downloads. Isso não deve ser contornado com certificados de teste.
