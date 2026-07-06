package dev.minebot.bridge.transport;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import dev.minebot.bridge.version.SectionSnapshot;

import java.io.ByteArrayOutputStream;
import java.util.Base64;
import java.util.zip.DeflaterOutputStream;

final class SectionKeyframeEncoder {
    private SectionKeyframeEncoder() {
    }

    static JsonObject encode(SectionSnapshot snapshot) {
        JsonObject message = new JsonObject();
        message.addProperty("channel", "world-stream");
        message.addProperty("type", "SECTION_KEYFRAME");
        message.addProperty("sub_id", snapshot.subId());
        message.addProperty("dimension", snapshot.dimension());
        JsonArray section = new JsonArray();
        section.add(snapshot.sectionX());
        section.add(snapshot.sectionY());
        section.add(snapshot.sectionZ());
        message.add("section", section);
        JsonArray size = new JsonArray();
        size.add(16);
        size.add(16);
        size.add(16);
        message.add("size", size);

        JsonArray paletteJson = new JsonArray();
        for (String state : snapshot.palette()) {
            paletteJson.add(state);
        }
        message.add("palette", paletteJson);

        int[] indices = snapshot.indices();
        String encoding = snapshot.palette().size() <= 256 ? "base64-deflate-u8" : "base64-deflate-u16le";
        message.addProperty("encoding", encoding);
        message.addProperty("indices", encodeIndices(indices, "base64-deflate-u8".equals(encoding)));
        return message;
    }

    private static String encodeIndices(int[] indices, boolean u8) {
        try {
            ByteArrayOutputStream raw = new ByteArrayOutputStream(indices.length * (u8 ? 1 : 2));
            for (int index : indices) {
                if (u8) {
                    raw.write(index & 0xFF);
                } else {
                    raw.write(index & 0xFF);
                    raw.write((index >>> 8) & 0xFF);
                }
            }
            ByteArrayOutputStream compressed = new ByteArrayOutputStream();
            try (DeflaterOutputStream deflater = new DeflaterOutputStream(compressed)) {
                raw.writeTo(deflater);
            }
            return Base64.getEncoder().encodeToString(compressed.toByteArray());
        } catch (java.io.IOException ex) {
            throw new IllegalStateException("failed to encode section keyframe", ex);
        }
    }
}

