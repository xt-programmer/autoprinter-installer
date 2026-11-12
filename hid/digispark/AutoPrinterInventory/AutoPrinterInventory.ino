// Digispark ATtiny85 + DigiKeyboard. US keyboard layout.
// Log in and run sudo -v manually before inserting. No passwords stored.
// Verify actual USB IDs match the generated --hid-vid/--hid-pid.
#include "DigiKeyboard.h"
#include <avr/pgmspace.h>
const char launchCommand[] PROGMEM = "bash -o pipefail -c 'curl --proto '\"'\"'=https'\"'\"' --proto-redir '\"'\"'=https'\"'\"' -fSL https://raw.githubusercontent.com/xt-programmer/autoprinter-installer/v0.5.0/install.sh | sudo -n sh -s -- --inventory'; ap_rc=$?; printf \"\\nAutoPrinter exit=%s\\n\" \"$ap_rc\"; (exit \"$ap_rc\")";
void setup() {
  DigiKeyboard.delay(5000);
  DigiKeyboard.sendKeyStroke(0);
  for (unsigned int i = 0; i < sizeof(launchCommand)-1; ++i) {
    DigiKeyboard.write(pgm_read_byte(launchCommand+i));
    DigiKeyboard.delay(20);
  }
  // Review command and press Enter manually. No automatic execution.
}
void loop() { DigiKeyboard.delay(1000); }
