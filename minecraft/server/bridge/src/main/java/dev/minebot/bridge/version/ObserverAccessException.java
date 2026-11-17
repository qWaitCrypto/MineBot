package dev.minebot.bridge.version;

public final class ObserverAccessException extends RuntimeException {
    private final String code;
    private final boolean retryable;

    public ObserverAccessException(String code, String message, boolean retryable) {
        super(message);
        this.code = code;
        this.retryable = retryable;
    }

    public String code() {
        return code;
    }

    public boolean retryable() {
        return retryable;
    }
}
