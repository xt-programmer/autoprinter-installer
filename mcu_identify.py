#!/usr/bin/env python3
"""Read Klipper's identify dictionary, without loading printer.cfg or GPIOs.

Runs in a child process with a timeout. Caller requires inactive Klipper and an
isolated, exact USB target. Optional explicit reset is gated by the exact built
dictionary hash, for verification that a flashed image survives an MCU reset.
"""
import argparse,hashlib,json,logging,sys
from pathlib import Path


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--klippy',type=Path,required=True); p.add_argument('--serial',required=True)
    p.add_argument('--reset-after-identify',action='store_true')
    p.add_argument('--expected-dictionary-sha256')
    args=p.parse_args(argv)
    sys.path.insert(0,str(args.klippy))
    import reactor,serialhdl,serial
    loop=reactor.Reactor(); reader=serialhdl.SerialReader(loop)
    result={}; errors=[]
    def query(eventtime):
        try:
            port=serial.Serial(args.serial,baudrate=250000,timeout=0,exclusive=True)
            if not reader._start_session(port): raise RuntimeError('MCU identify timed out')
            raw=reader.get_msgparser().get_raw_data_dictionary()
            result.update(json.loads(raw))
            if args.reset_after_identify:
                if hashlib.sha256(raw).hexdigest()!=args.expected_dictionary_sha256:
                    raise RuntimeError('Reset refused: live dictionary does not match the explicitly supplied image')
                reader.send('reset')
                loop.pause(loop.monotonic()+.5)
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
