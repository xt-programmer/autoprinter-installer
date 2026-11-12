#!/usr/bin/env python3
"""Read Klipper's identify dictionary, without loading printer.cfg or GPIOs.

Runs in a child process with a timeout. Caller requires inactive Klipper and an
isolated, exact USB target. Optional explicit reset is gated by the exact built
dictionary hash, for verification that a flashed image survives an MCU reset.
"""
import argparse,ast,hashlib,json,logging,sys
from pathlib import Path


def check_host_protocol(klippy,parser):
    """Resolve core host formats without configuring pins or loading printer.cfg."""
    formats={'emergency_stop','reset','get_clock','get_uptime'}
    found=False
    tree=ast.parse((klippy/'mcu.py').read_text(encoding='utf-8'))
    for method in ast.walk(tree):
        if isinstance(method,(ast.FunctionDef,ast.AsyncFunctionDef)) and method.name=='_send_get_config':
            for node in ast.walk(method):
                if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=='lookup_query_command':
                    args=node.args[:2]
                    if len(args)==2 and all(isinstance(x,ast.Constant) and isinstance(x.value,str) for x in args):
                        formats.update(x.value for x in args); found=True
    if not found: raise RuntimeError('Unsupported host core layout; cannot verify get_config protocol')
    for fmt in sorted(formats): parser.lookup_command(fmt)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--klippy',type=Path,required=True); p.add_argument('--serial',required=True)
    p.add_argument('--reset-after-identify',action='store_true')
    p.add_argument('--expected-dictionary-sha256')
    p.add_argument('--expected-dictionary-file',type=Path)
    p.add_argument('--check-host-protocol',action='store_true')
    args=p.parse_args(argv)
    sys.path.insert(0,str(args.klippy))
    import reactor,serialhdl,serial,msgproto
    loop=reactor.Reactor(); reader=serialhdl.SerialReader(loop)
    result={}; errors=[]
    def query(eventtime):
        try:
            port=serial.Serial(args.serial,baudrate=250000,timeout=0,exclusive=True)
            if not reader._start_session(port): raise RuntimeError('MCU identify timed out')
            raw=reader.get_msgparser().get_raw_data_dictionary()
            result.update(json.loads(raw))
            if args.check_host_protocol: check_host_protocol(args.klippy,reader.get_msgparser())
            if args.reset_after_identify:
                matches=(json.loads(raw)==json.loads(args.expected_dictionary_file.read_bytes())) if args.expected_dictionary_file else hashlib.sha256(raw).hexdigest()==args.expected_dictionary_sha256
                if not matches:
                    raise RuntimeError('Reset refused: live dictionary does not match the explicitly supplied image')
                reader.send('reset')
                loop.pause(loop.monotonic()+.5)
        except msgproto.error as error:
            print('MCU_PROTOCOL_ERROR '+str(error),flush=True); errors.append(str(error))
        except Exception as error: errors.append(str(error))
        finally:
            reader.disconnect(); loop.end()
        return loop.NEVER
    loop.register_callback(query)
    try: loop.run()
    finally: loop.finalize()
    if errors: raise RuntimeError('; '.join(errors))
    print('MCU_IDENTIFY_JSON '+json.dumps(result,sort_keys=True),flush=True)


if __name__=='__main__': main()
