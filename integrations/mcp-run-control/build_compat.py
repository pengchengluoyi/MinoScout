"""Build a clearly labeled compatibility adaptation of upstream MCP Run Control 0.1.2."""
import argparse
import io
import struct
import subprocess
import tempfile
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

old_owner = b'com/intellij/mcpserver/util/Execution_utilKt'
new_owner = b'dev/lewik/jetbrainsmcp/runcontrol/CompatibilitySessionIds'

def change_owner(blob):
    if blob[:4]!=b'\xca\xfe\xba\xbe':raise ValueError('Not a JVM class')
    count=struct.unpack_from('>H',blob,8)[0];offset=10;index=1;result=bytearray(blob[:10]);replaced=0
    sizes={3:4,4:4,5:8,6:8,7:2,8:2,9:4,10:4,11:4,12:4,15:3,16:2,17:4,18:4,19:2,20:2}
    while index<count:
        tag=blob[offset];offset+=1;result.append(tag)
        if tag==1:
            length=struct.unpack_from('>H',blob,offset)[0];offset+=2
            value=blob[offset:offset+length];offset+=length
            if value==old_owner:value=new_owner;replaced+=1
            result.extend(struct.pack('>H',len(value)));result.extend(value)
        else:
            size=sizes[tag];result.extend(blob[offset:offset+size]);offset+=size
            if tag in (5,6):index+=1
        index+=1
    if replaced!=1:raise ValueError(f'Unexpected upstream method owner matches: {replaced}')
    result.extend(blob[offset:]);return bytes(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--upstream', required=True, type=Path, help='Unmodified upstream 0.1.2 release ZIP')
    parser.add_argument('--ide-home', required=True, type=Path, help='Installed PyCharm app or Contents directory')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        raise FileExistsError('Output exists; choose a new path')
    ide = args.ide_home.resolve()
    if (ide / 'Contents').is_dir():
        ide /= 'Contents'
    source_dir = Path(__file__).resolve().parent
    compiler = ide / 'jbr/Contents/Home/bin/javac'
    if not compiler.is_file():
        compiler = ide / 'jbr/bin/javac'
    classpath = ':'.join(map(str, (ide / 'lib').glob('*.jar')))
    with tempfile.TemporaryDirectory(prefix='mcp-run-control-compat-') as temporary:
        classes = Path(temporary)
        subprocess.run([str(compiler), '--release', '21', '-encoding', 'UTF-8', '-cp', classpath,
                        '-d', str(classes), str(source_dir / 'CompatibilitySessionIds.java')], check=True)
        with ZipFile(args.upstream) as upstream_zip:
            member = 'mcp-run-control/lib/mcp-run-control-0.1.2.jar'
            jar_bytes = upstream_zip.read(member)
            built = io.BytesIO()
            with ZipFile(io.BytesIO(jar_bytes)) as upstream, ZipFile(built, 'w', ZIP_DEFLATED) as target:
                metadata = upstream.read('META-INF/plugin.xml').decode()
                if '<version>0.1.2</version>' not in metadata:
                    raise ValueError('Expected the unmodified upstream 0.1.2 JAR')
                if any(n.endswith(('.SF', '.RSA', '.DSA')) for n in upstream.namelist()):
                    raise ValueError('Do not rewrite a signed upstream JAR')
                for info in upstream.infolist():
                    content = upstream.read(info.filename)
                    if info.filename == 'dev/lewik/jetbrainsmcp/runcontrol/RunControlToolset.class':
                        content = change_owner(content)
                    if info.filename == 'META-INF/plugin.xml':
                        content = content.decode().replace('<version>0.1.2</version>', '<version>0.1.2-local261.1</version>')
                        content = content.replace('<name>MCP Run Control</name>', '<name>MCP Run Control (local 261 compatibility)</name>')
                        content = content.replace('since-build="261"', 'since-build="261" until-build="261.*"')
                        content = content.replace('<description><![CDATA[', '<description><![CDATA[Local compatibility adaptation for PyCharm 261. Adds a session-ID shim for the missing bundled MCP buildSessionId API. Upstream stop/list logic is unchanged; this is not the original upstream binary. ')
                        content = content.encode()
                    target.writestr(info, content)
                helper = 'dev/lewik/jetbrainsmcp/runcontrol/CompatibilitySessionIds.class'
                target.writestr(helper, (classes / helper).read_bytes())
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with ZipFile(args.output, 'x', ZIP_DEFLATED) as output:
                for info in upstream_zip.infolist():
                    output.writestr(info, built.getvalue() if info.filename == member else upstream_zip.read(info.filename))
                output.writestr('mcp-run-control/LICENSE', (source_dir / 'LICENSE').read_bytes())
                output.writestr('mcp-run-control/NOTICE', 'Based on Lewik/mcp-run-control v0.1.2 (Apache-2.0). Local PyCharm 261 compatibility adaptation: replace the unavailable bundled MCP buildSessionId call with a local session ID helper. The upstream list/stop tools are unchanged. This is a locally adapted binary, not the upstream release.\n')
    print(args.output)


if __name__ == '__main__':
    main()
