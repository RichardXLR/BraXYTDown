# Avisos e licenças de terceiros

O BraXYTDow distribui ou pode baixar as ferramentas abaixo. Elas permanecem
programas independentes e são iniciadas como processos externos; não são
vinculadas às bibliotecas Python do BraXYTDow. A versão e o SHA-256 exatos dos
binários incorporados estão em `bin/tools-manifest.json` e no manifesto da
release.

## yt-dlp

- Projeto e código-fonte: https://github.com/yt-dlp/yt-dlp
- Avisos completos do executável: https://github.com/yt-dlp/yt-dlp/blob/master/THIRD_PARTY_LICENSES.txt
- Licenciamento upstream: https://github.com/yt-dlp/yt-dlp#licensing
- Licença aplicável ao executável standalone distribuído: GPL-3.0-or-later,
  conforme documentado pelo próprio projeto (o código principal usa Unlicense,
  mas o executável PyInstaller incorpora componentes adicionais).
- Texto da GPL 3: https://www.gnu.org/licenses/gpl-3.0.txt
- Releases stable: https://github.com/yt-dlp/yt-dlp/releases
- Releases nightly: https://github.com/yt-dlp/yt-dlp-nightly-builds/releases

## FFmpeg e FFprobe (Gyan Essentials)

- Projeto e código-fonte: https://ffmpeg.org/
- Página legal: https://ffmpeg.org/legal.html
- Build Windows, configuração, checksum e link do código-fonte correspondente:
  https://www.gyan.dev/ffmpeg/builds/
- Espelho dos builds release: https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-github
- Licença deste build estático Essentials: GPL-3.0. A própria saída de
  `ffmpeg -L` incluída no binário apresenta o aviso de licença.
- Texto da GPL 3: https://www.gnu.org/licenses/gpl-3.0.txt

O pacote Gyan pode incluir codecs e bibliotecas de terceiros sob licenças
compatíveis. A configuração integral da compilação pode ser consultada com
`ffmpeg -version`; a página do build lista as bibliotecas e aponta para o commit
de FFmpeg usado em cada release.

## Deno

- Projeto e código-fonte: https://github.com/denoland/deno
- Licença MIT e avisos: https://github.com/denoland/deno/blob/main/LICENSE.md
- Releases oficiais: https://github.com/denoland/deno/releases

Deno é um runtime JavaScript opcional utilizado pelo yt-dlp para compatibilidade
com desafios recentes do YouTube. O BraXYTDow valida o arquivo `.sha256sum`
publicado junto ao asset antes de instalá-lo.

## Provedores opcionais de PO Token

O BraXYTDow não incorpora nem instala um provedor por padrão. Quando o usuário
habilita explicitamente o recurso, somente os projetos destacados pelo guia do
yt-dlp e presentes na lista interna podem ser aceitos:

- bgutil-ytdlp-pot-provider: https://github.com/Brainicism/bgutil-ytdlp-pot-provider
- yt-dlp-getpot-wpc: https://github.com/coletdjnz/yt-dlp-getpot-wpc

O pacote continua sujeito à licença e aos avisos publicados em seu repositório.
Antes de ativar, o aplicativo exige HTTPS, origem esperada, SHA-256, estrutura
de plugin válida e teste funcional em quarentena. Se cookies e um provedor forem
ativados ao mesmo tempo, o provedor será carregado no mesmo processo externo do
yt-dlp e poderá participar das requisições autenticadas; não o ative sem necessidade.

## Componentes do aplicativo empacotado

- Python: PSF License — https://docs.python.org/3/license.html
- PySide6 / Qt for Python: LGPLv3/GPLv3/comercial, conforme o componente —
  https://doc.qt.io/qtforpython-6/licenses.html
- Qt: avisos e fontes — https://www.qt.io/licensing/open-source-lgpl-obligations
- PyInstaller: GPLv2 com exceção para distribuir aplicações empacotadas —
  https://pyinstaller.org/en/stable/license.html

As DLLs do Qt permanecem arquivos separados no pacote onedir. No EXE onefile,
o bootloader do PyInstaller as extrai como arquivos separados antes da execução.
Nenhuma biblioteca Qt foi modificada por este projeto.

Este documento é informativo e não substitui os textos oficiais das licenças.
