# Build the Firefox extensions into XPI files ready for deployment
# Requires: zip, jq (optional for version bumping)

EXTENSIONS = cookie_monitor keylogger
BUILD_DIR = build

.PHONY: all clean $(EXTENSIONS)

all: $(EXTENSIONS)

$(EXTENSIONS):
	@echo "Building $@ extension..."
	@mkdir -p $(BUILD_DIR)
	@cd $@ && zip -r ../$(BUILD_DIR)/$@.xpi . -x "*.git*" -x "*.DS_Store"
	@echo "  -> $(BUILD_DIR)/$@.xpi"

clean:
	@rm -rf $(BUILD_DIR)
	@echo "Cleaned build directory"

# Quick build without all the noise
quick:
	@mkdir -p $(BUILD_DIR)
	@for ext in $(EXTENSIONS); do \
		echo "Building $$ext..."; \
		cd $$ext && zip -qr ../$(BUILD_DIR)/$$ext.xpi . -x "*.git*" -x "*.DS_Store" && cd ..; \
	done
	@echo "Done. Extensions in $(BUILD_DIR)/"
