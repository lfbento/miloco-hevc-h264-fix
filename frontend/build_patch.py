#!/usr/bin/env python3
"""Gera o bundle do Miloco patchado (PT-BR + player H.264 universal).

Uso: python3 build_patch.py <bundle-original.js> [saida.js]

- bundle-original.js: copie do container o JS servido em
  /app/miloco_server/static/assets/js/index-<hash>.js (o hash muda a cada
  release; os patches têm asserts que falham se o upstream mudar demais).
- Saída padrão: ./dist/<mesmo-nome>.js — monte por cima do arquivo do
  container (ver docker-compose.example.yml).

O patch do player resolve dois bugs que impedem a reprodução de câmeras
H.264 (ex.: após transcodificação HEVC->H.264 no backend):
1. configure() adiado até o 1º frame (o codec só é conhecido depois do
   farejador; antes era forçado hev1 — que o Chrome/Linux nem suporta);
2. I() (detecção de keyframe) no ramo h264 só olhava o 1º NAL — mensagens
   [SPS][PPS][SEI][IDR] eram descartadas e o player ficava num loading
   eterno sem erro nenhum.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC_BUNDLE = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "index-original.js"
PT_DICT = HERE / "translation-pt.json"
OUT_BUNDLE = Path(sys.argv[2]) if len(sys.argv) > 2 else HERE / "dist" / SRC_BUNDLE.name


def keys_of(d, prefix=""):
    out = set()
    for k, v in d.items():
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out |= keys_of(v, path)
        else:
            out.add(path)
    return out


def main():
    js = SRC_BUNDLE.read_text(encoding="utf-8")
    pt = json.loads(PT_DICT.read_text(encoding="utf-8"))

    # 1. injeta o dicionário pt no mapa de recursos do i18next
    old_xk = "xk={zh:{translation:uk},en:{translation:dk}}"
    new_xk = "xk={zh:{translation:uk},en:{translation:dk},pt:{translation:" + json.dumps(pt, ensure_ascii=False, separators=(",", ":")) + "}}"
    assert js.count(old_xk) == 1, f"xk: esperado 1 ocorrência, achou {js.count(old_xk)}"

    # 2. adiciona "Português" nas listas de idioma (2 lugares)
    old_opt = '{key:"zh",label:"简体中文"},{key:"en",label:"English"}'
    new_opt = '{key:"zh",label:"简体中文"},{key:"en",label:"English"},{key:"pt",label:"Português"}'
    n_opt = js.count(old_opt)
    assert n_opt >= 1, "lista de idiomas não encontrada"

    # 3. detecção automática: aceita 'pt' e usa como padrão (navegador do Bento é pt-BR)
    old_detect = '["zh","en"].includes(r)?r:"zh"'
    new_detect = '["zh","en","pt"].includes(r)?r:"pt"'
    assert js.count(old_detect) == 1, f"detect: esperado 1 ocorrência, achou {js.count(old_detect)}"

    # 3b. CORREÇÃO DO IDIOMA PADRÃO: Ck() devolve Pd.getState().language antes
    #     de chegar na detecção do navegador, e o estado inicial do store é
    #     language:"zh" — então TODO navegador novo caía em chinês, mesmo com o
    #     patch 3. O default do store agora detecta do navegador (pt por padrão).
    #     Escolha explícita do usuário (persistida no store) continua valendo.
    old_lang = '({language:"zh",themeMode:"light"'
    new_lang = ('({language:(()=>{try{const r=(navigator.language||navigator.userLanguage||"").split("-")[0];'
                'return["zh","en","pt"].includes(r)?r:"pt"}catch(e){return"pt"}})(),themeMode:"light"')
    assert js.count(old_lang) == 1, f"store lang: esperado 1 ocorrência, achou {js.count(old_lang)}"

    # 3c. fallback i18next: chave faltando no pt caía em "zh" (chinês) — melhor
    #     cair no inglês (dicionário upstream completo) e só então no zh.
    old_fb = 'fallbackLng:"zh"'
    new_fb = 'fallbackLng:["en","zh"]'
    assert js.count(old_fb) == 1, f"fallbackLng: esperado 1 ocorrência, achou {js.count(old_fb)}"

    # 4. player: com o transcoder H.264 no backend o codec só é conhecido depois
    #    do 1º frame — a configuração inicial é adiada pro farejador (4d)
    old_cfg = 'f.current.configure({codec:e,hardwareAcceleration:"prefer-hardware"})'
    new_cfg = 'void 0'
    n_cfg = js.count(old_cfg)
    assert n_cfg == 1, f"configure: esperado 1 ocorrência, achou {n_cfg}"

    # 4b. error callback do VideoDecoder: loga e exibe a causa real
    old_err = 'error:()=>{v(l("instant.deviceList.deviceDecodeFailed")),Le.error(l("instant.deviceList.deviceDecodeFailed"))}'
    new_err = (
        'error:Q=>{console.error("VideoDecoder error:",Q),'
        'v(l("instant.deviceList.deviceDecodeFailed")+" — "+(Q&&Q.message||Q)),'
        'Le.error(l("instant.deviceList.deviceDecodeFailed")+" — "+(Q&&Q.message||Q))}'
    )
    n_err = js.count(old_err)
    assert n_err == 1, f"error handler: esperado 1 ocorrência, achou {n_err}"

    # 4c. catch do decode(): idem
    old_catch = '}catch{v(l("instant.deviceList.deviceDecodeFailed"))}}'
    new_catch = (
        '}catch(Q){console.error("decode() falhou:",Q),'
        'v(l("instant.deviceList.deviceDecodeFailed")+" — "+(Q&&Q.message||Q)),'
        'Le.error(l("instant.deviceList.deviceDecodeFailed")+" — "+(Q&&Q.message||Q))}}'
    )
    n_catch = js.count(old_catch)
    assert n_catch == 1, f"decode catch: esperado 1 ocorrência, achou {n_catch}"

    # 4d. o configure agora acontece no 1º frame, com o codec farejado:
    #     `_cfgd` guarda o codec configurado (state do React ainda não atualizou)
    old_sniff = 'if(!b){const K=M0e(A);K!=="unknown"&&E(K==="h264"?"avc1.42E01E":"hvc1.1.6.L93.B0")}'
    new_sniff = (
        'if(!b){const K=M0e(A);if(K!=="unknown"&&!f.current._cfgd){'
        'const C=K==="h264"?"avc1.42C01F":"hvc1.1.6.L93.B0";f.current._cfgd=C;E(C);'
        'try{f.current.configure({codec:C,hardwareAcceleration:"prefer-software"})}catch(cf){'
        'try{f.current.configure({codec:C})}catch(cf2){console.error("configure falhou:",cf2),'
        'v(l("instant.deviceList.deviceDecodeFailed")+" — "+(cf2&&cf2.message||cf2)),'
        'Le.error(l("instant.deviceList.deviceDecodeFailed")+" — "+(cf2&&cf2.message||cf2))}}'
        'f.current._waitForKeyFrame=!0}}'
    )
    n_sniff = js.count(old_sniff)
    assert n_sniff == 1, f"sniff: esperado 1 ocorrência, achou {n_sniff}"

    # 4f. I() (detecção de keyframe) no ramo h264 só olhava o 1º NAL: nas
    #     mensagens do transcoder o keyframe vem como [SPS][PPS][SEI][IDR] e o
    #     SPS (tipo 7) fazia o player descartar TODOS os frames em silêncio
    #     (loading eterno). Varre todos os NALs como o ramo h265 já fazia.
    old_key = ('I=($,R)=>{if(R.startsWith("avc1")||R.startsWith("h264")){let M=0;'
               'for(;M<$.length-4;){if($[M]===0&&$[M+1]===0&&($[M+2]===0&&$[M+3]===1||$[M+2]===1))'
               'return($[M+2]===1?$[M+3]&31:$[M+4]&31)===5;M++}return!1}')
    new_key = ('I=($,R)=>{if(R.startsWith("avc1")||R.startsWith("h264")){let M=0;'
               'for(;M<$.length-4;){if($[M]===0&&$[M+1]===0&&($[M+2]===0&&$[M+3]===1||$[M+2]===1))'
               '{const T=($[M+2]===1?$[M+3]:$[M+4])&31;if(T===5||T===7)return!0}M++}return!1}')
    n_key = js.count(old_key)
    assert n_key == 1, f"keyframe I(): esperado 1 ocorrência, achou {n_key}"

    # 4e. detecção de keyframe usa o codec configurado (state `b` só atualiza
    #     depois do render — sem isso o 1º keyframe seria descartado)
    old_d = 'const D=b||e;'
    new_d = 'const D=f.current._cfgd||b||e;'
    n_d = js.count(old_d)
    assert n_d == 1, f"codec D: esperado 1 ocorrência, achou {n_d}"

    js = js.replace(old_xk, new_xk)
    js = js.replace(old_opt, new_opt)
    js = js.replace(old_detect, new_detect)
    js = js.replace(old_lang, new_lang)
    js = js.replace(old_fb, new_fb)
    js = js.replace(old_cfg, new_cfg)
    js = js.replace(old_err, new_err)
    js = js.replace(old_catch, new_catch)
    js = js.replace(old_sniff, new_sniff)
    js = js.replace(old_key, new_key)
    js = js.replace(old_d, new_d)

    OUT_BUNDLE.write_text(js, encoding="utf-8")
    print(f"OK: {OUT_BUNDLE} ({len(js)} bytes) | opções de idioma: {n_opt} | PT keys: {len(keys_of(pt))}")


if __name__ == "__main__":
    sys.exit(main())
