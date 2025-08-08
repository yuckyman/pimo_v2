# PiMO login splash (SSH only)
if [ -n "$SSH_CONNECTION" ] && [ -x "$HOME/.local/bin/pimo_splash.py" ] && [ -z "$PIMO_SPLASH_SHOWN" ]; then
  export PIMO_SPLASH_SHOWN=1
  "$HOME/.local/bin/pimo_splash.py" || true
fi
