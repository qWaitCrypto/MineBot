package dev.minebot.camera.client;

import dev.minebot.camera.protocol.CameraClientTelemetryPayload;
import dev.minebot.camera.protocol.CameraDirectivePayload;
import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.networking.v1.ClientPlayConnectionEvents;
import net.fabricmc.fabric.api.client.networking.v1.ClientPlayNetworking;
import net.fabricmc.fabric.api.networking.v1.PayloadTypeRegistry;

public final class MineBotCameraClient implements ClientModInitializer {
    public static final String MOD_ID = "minebot-camera-client";

    @Override
    public void onInitializeClient() {
        PayloadTypeRegistry.clientboundPlay().register(CameraDirectivePayload.TYPE, CameraDirectivePayload.CODEC);
        PayloadTypeRegistry.serverboundPlay().register(
            CameraClientTelemetryPayload.TYPE,
            CameraClientTelemetryPayload.CODEC
        );
        ClientPlayNetworking.registerGlobalReceiver(CameraDirectivePayload.TYPE, (payload, context) ->
            context.client().execute(() -> CameraController.instance().accept(context.client(), payload.directive()))
        );
        ClientPlayConnectionEvents.DISCONNECT.register((handler, client) ->
            CameraController.instance().disconnect(client)
        );
    }
}
