PYTHON ?= python3

# Usage:
#   make iv TICKER=AAPL [LOOKBACK=252] [TARGET_DTE=30] [VERBOSE=1] [NOWRITE=1] [API_KEY=...]

.PHONY: iv
iv:
	@if [ -z "$(TICKER)" ]; then \
	  echo "Usage: make iv TICKER=SYMBOL [LOOKBACK=252] [TARGET_DTE=30] [VERBOSE=1] [NOWRITE=1] [API_KEY=...]"; \
	  exit 2; \
	fi
	@cmd="$(PYTHON) iv_metrics.py --ticker $(TICKER)"; \
	[ -n "$(LOOKBACK)" ] && cmd="$$cmd --lookback $(LOOKBACK)"; \
	[ -n "$(TARGET_DTE)" ] && cmd="$$cmd --target-dte $(TARGET_DTE)"; \
	[ -n "$(VERBOSE)" ] && cmd="$$cmd --verbose"; \
	[ -n "$(NOWRITE)" ] && cmd="$$cmd --no-write-history"; \
	[ -n "$(API_KEY)" ] && cmd="$$cmd --api-key $(API_KEY)"; \
	echo "$$cmd"; \
	eval "$$cmd"

