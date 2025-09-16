#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <libusb-1.0/libusb.h>

#include <unistd.h>

#define USB_VID_SIPEED 0x359f
#define USB_PID_SLOGIC16U3 0x3031

#define SLOGIC16U3_CONTROL_IN_REQ_REG_READ 0x00
#define SLOGIC16U3_CONTROL_OUT_REQ_REG_WRITE 0x01

#define SLOGIC16U3_R32_CTRL 0x0004
#define SLOGIC16U3_R32_FLAG 0x0008
#define SLOGIC16U3_R32_AUX 0x000c

#define NUM_TRANSFERS 4
#define BULK_TIMEOUT 1000
#define TRANSFER_SIZE 4096*512

// 设备上下文结构
typedef struct {
    libusb_device_handle *dev_handle;
    uint16_t cur_samplechannel;
    uint64_t cur_samplerate;
    double voltage_threshold[2];


    libusb_context *ctx;
    unsigned char endpoint;
    struct libusb_transfer *transfers[NUM_TRANSFERS];
    int active_transfers;
    int should_stop;
} slogic16u3_context;

// error: redefinition of ‘__uint16_identity’
// // 辅助函数
// static inline uint16_t htole16(uint16_t value)
// {
//     const union {
//         uint16_t val;
//         uint8_t bytes[2];
//     } u = { .val = 0x1234 };
//     return (u.bytes[0] == 0x34) ? value : ((value & 0xFF) << 8) | ((value >> 8) & 0xFF);
// }

// USB控制写操作
static int slogic_usb_control_write(libusb_device_handle *dev_handle,
                                   uint8_t request, uint16_t value,
                                   uint16_t index, uint8_t *data, size_t len,
                                   int timeout)
{
    int ret;
    
    // printf("Control Write: req:%u value:%u index:%u len:%zu timeout:%dms\n",
    //        request, value, index, len, timeout);
           
    if (!data && len) {
        printf("Warning: Nothing to write although len(%zu)>0!\n", len);
        len = 0;
    } else if (len & 0x3) {
        size_t len_aligndup = (len + 0x3) & (~0x3);
        // printf("Warning: Align up to %zu(from %zu)!\n", len_aligndup, len);
        len = len_aligndup;
    }

    int total_written = 0;
    for (size_t i = 0; i < len; i += 4) {
        int written = libusb_control_transfer(
            dev_handle,
            LIBUSB_REQUEST_TYPE_VENDOR | LIBUSB_ENDPOINT_OUT,
            request, value + i, index, data + i, 4, timeout);
            
        if (written < 0) {
            printf("Error: Control write failed: %s\n", libusb_error_name(written));
            return written;
        }
        total_written += written;
    }

    return total_written;
}

// USB控制读操作
static int slogic_usb_control_read(libusb_device_handle *dev_handle,
                                  uint8_t request, uint16_t value,
                                  uint16_t index, uint8_t *data, size_t len,
                                  int timeout)
{
    int ret;
    
    // printf("Control Read: req:%u value:%u index:%u len:%zu timeout:%dms\n",
    //        request, value, index, len, timeout);
           
    if (!data && len) {
        printf("Error: Can't read to NULL while len(%zu)>0!\n", len);
        return -1;
    } else if (len & 0x3) {
        size_t len_aligndup = (len + 0x3) & (~0x3);
        // printf("Warning: Align up to %zu(from %zu)!\n", len_aligndup, len);
        len = len_aligndup;
    }

    int total_read = 0;
    for (size_t i = 0; i < len; i += 4) {
        int read = libusb_control_transfer(
            dev_handle,
            LIBUSB_REQUEST_TYPE_VENDOR | LIBUSB_ENDPOINT_IN,
            request, value + i, index, data + i, 4, timeout);
            
        if (read < 0) {
            printf("Error: Control read failed: %s\n", libusb_error_name(read));
            return read;
        }
        total_read += read;
    }

    return total_read;
}

// 设备复位
static int slogic16u3_reset(libusb_device_handle *dev_handle)
{
    const uint8_t cmd_rst[] = { 0x02, 0x00, 0x00, 0x00 };
    const uint8_t cmd_derst[] = { 0x00, 0x00, 0x00, 0x00 };

    int ret = slogic_usb_control_write(dev_handle, 
                                     SLOGIC16U3_CONTROL_OUT_REQ_REG_WRITE,
                                     SLOGIC16U3_R32_CTRL, 0x0000,
                                     (uint8_t*)cmd_rst, sizeof(cmd_rst), 500);
    if (ret < 0) return ret;
    
    return slogic_usb_control_write(dev_handle, 
                                  SLOGIC16U3_CONTROL_OUT_REQ_REG_WRITE,
                                  SLOGIC16U3_R32_CTRL, 0x0000,
                                  (uint8_t*)cmd_derst, sizeof(cmd_derst), 500);
}

// 设置测试模式
static int slogic16u3_set_test_mode(libusb_device_handle *dev_handle, uint32_t mode)
{
    uint8_t cmd_aux[64] = { 0 };
    size_t retry = 0;
    
    // 配置AUX寄存器
    *(uint32_t *)(cmd_aux) = 0x00000005;
    int ret = slogic_usb_control_write(dev_handle,
                                     SLOGIC16U3_CONTROL_OUT_REQ_REG_WRITE,
                                     SLOGIC16U3_R32_AUX, 0x0000, cmd_aux, 4, 500);
    if (ret < 0) return ret;

    // 等待配置完成
    do {
        ret = slogic_usb_control_read(dev_handle,
                                    SLOGIC16U3_CONTROL_IN_REQ_REG_READ,
                                    SLOGIC16U3_R32_AUX, 0x0000, cmd_aux, 4, 500);
        if (ret < 0) return ret;
        
        printf("[%zu] Read testmode: %08x\n", retry, *(uint32_t*)cmd_aux);
        retry++;
        
        if (retry > 5) {
            printf("Error: Timeout waiting for test mode configuration\n");
            return -1;
        }
    } while (!(cmd_aux[2] & 0x01));

    // 读取当前配置
    uint16_t aux_length = (*(uint16_t *)cmd_aux) >> 9;
    printf("Test mode length: %u\n", aux_length);
    
    ret = slogic_usb_control_read(dev_handle,
                                SLOGIC16U3_CONTROL_IN_REQ_REG_READ,
                                SLOGIC16U3_R32_AUX + 4, 0x0000,
                                cmd_aux + 4, aux_length, 500);
    if (ret < 0) return ret;

    printf("Current AUX: %u %u %u %u %08x\n", 
           cmd_aux[0], cmd_aux[1], cmd_aux[2], cmd_aux[3], 
           *(uint32_t*)(cmd_aux + 4));

    // 设置新模式
    *(uint32_t*)(cmd_aux + 4) = mode;
    
    printf("Setting AUX: %u %u %u %u %08x\n", 
           cmd_aux[0], cmd_aux[1], cmd_aux[2], cmd_aux[3], 
           *(uint32_t*)(cmd_aux + 4));
           
    ret = slogic_usb_control_write(dev_handle,
                                 SLOGIC16U3_CONTROL_OUT_REQ_REG_WRITE,
                                 SLOGIC16U3_R32_AUX + 4, 0x0000,
                                 cmd_aux + 4, aux_length, 500);
    if (ret < 0) return ret;

    // 验证配置
    ret = slogic_usb_control_read(dev_handle,
                                SLOGIC16U3_CONTROL_IN_REQ_REG_READ,
                                SLOGIC16U3_R32_AUX + 4, 0x0000,
                                cmd_aux + 4, aux_length, 500);
    if (ret < 0) return ret;

    printf("Final AUX: %u %u %u %u %08x\n", 
           cmd_aux[0], cmd_aux[1], cmd_aux[2], cmd_aux[3], 
           *(uint32_t*)(cmd_aux + 4));

    if (mode != *(uint32_t*)(cmd_aux + 4)) {
        printf("Warning: Failed to configure test mode completely\n");
        return -1;
    }

    printf("Successfully configured test mode: 0x%08x\n", mode);
    return 0;
}

// 启动采集
static int slogic16u3_start_acquisition(slogic16u3_context *ctx)
{
    const uint8_t cmd_run[] = { 0x01, 0x00, 0x00, 0x00 };
    uint8_t cmd_aux[64] = { 0 };
    
    // 配置采样通道
    *(uint32_t *)(cmd_aux) = 0x00000001;
    int ret = slogic_usb_control_write(ctx->dev_handle,
                                     SLOGIC16U3_CONTROL_OUT_REQ_REG_WRITE,
                                     SLOGIC16U3_R32_AUX, 0x0000, cmd_aux, 4, 500);
    if (ret < 0) return ret;

    size_t retry = 0;
    do {
        ret = slogic_usb_control_read(ctx->dev_handle,
                                    SLOGIC16U3_CONTROL_IN_REQ_REG_READ,
                                    SLOGIC16U3_R32_AUX, 0x0000, cmd_aux, 4, 500);
        if (ret < 0) return ret;
        
        printf("[%zu] Read channel config: %08x\n", retry, *(uint32_t*)cmd_aux);
        retry++;
        
        if (retry > 5) {
            printf("Error: Timeout waiting for channel configuration\n");
            return -1;
        }
    } while (!(cmd_aux[2] & 0x01));

    uint16_t aux_length = (*(uint16_t *)cmd_aux) >> 9;
    printf("Channel config length: %u\n", aux_length);
    
    ret = slogic_usb_control_read(ctx->dev_handle,
                                SLOGIC16U3_CONTROL_IN_REQ_REG_READ,
                                SLOGIC16U3_R32_AUX + 4, 0x0000,
                                cmd_aux + 4, aux_length, 500);
    if (ret < 0) return ret;

    printf("Current channel AUX: %u %u %u %u %08x\n", 
           cmd_aux[0], cmd_aux[1], cmd_aux[2], cmd_aux[3], 
           *(uint32_t*)(cmd_aux + 4));

    // 设置通道掩码
    *(uint32_t*)(cmd_aux + 4) = (1 << ctx->cur_samplechannel) - 1;
    
    printf("Setting channel AUX: %u %u %u %u %08x\n", 
           cmd_aux[0], cmd_aux[1], cmd_aux[2], cmd_aux[3], 
           *(uint32_t*)(cmd_aux + 4));
           
    ret = slogic_usb_control_write(ctx->dev_handle,
                                 SLOGIC16U3_CONTROL_OUT_REQ_REG_WRITE,
                                 SLOGIC16U3_R32_AUX + 4, 0x0000,
                                 cmd_aux + 4, aux_length, 500);
    if (ret < 0) return ret;

    // 验证通道配置
    ret = slogic_usb_control_read(ctx->dev_handle,
                                SLOGIC16U3_CONTROL_IN_REQ_REG_READ,
                                SLOGIC16U3_R32_AUX + 4, 0x0000,
                                cmd_aux + 4, aux_length, 500);
    if (ret < 0) return ret;

    printf("Final channel AUX: %u %u %u %u %08x\n", 
           cmd_aux[0], cmd_aux[1], cmd_aux[2], cmd_aux[3], 
           *(uint32_t*)(cmd_aux + 4));

    if ((1 << ctx->cur_samplechannel) - 1 != *(uint32_t*)(cmd_aux + 4)) {
        printf("Warning: Channel configuration may not be complete\n");
    }

    // 配置采样率
    *(uint32_t *)(cmd_aux) = 0x00000002;
    ret = slogic_usb_control_write(ctx->dev_handle,
                                 SLOGIC16U3_CONTROL_OUT_REQ_REG_WRITE,
                                 SLOGIC16U3_R32_AUX, 0x0000, cmd_aux, 4, 500);
    if (ret < 0) return ret;

    retry = 0;
    do {
        ret = slogic_usb_control_read(ctx->dev_handle,
                                    SLOGIC16U3_CONTROL_IN_REQ_REG_READ,
                                    SLOGIC16U3_R32_AUX, 0x0000, cmd_aux, 4, 500);
        if (ret < 0) return ret;
        
        printf("[%zu] Read samplerate config: %08x\n", retry, *(uint32_t*)cmd_aux);
        retry++;
        
        if (retry > 5) {
            printf("Error: Timeout waiting for samplerate configuration\n");
            return -1;
        }
    } while (!(cmd_aux[2] & 0x01));

    aux_length = (*(uint16_t *)cmd_aux) >> 9;
    printf("Samplerate config length: %u\n", aux_length);

    // 读取和设置采样率
    memset(cmd_aux, 0, sizeof(cmd_aux));
    while (*(uint16_t*)(cmd_aux + 4) <= 1) {
        ret = slogic_usb_control_read(ctx->dev_handle,
                                    SLOGIC16U3_CONTROL_IN_REQ_REG_READ,
                                    SLOGIC16U3_R32_AUX + 4, 0x0000,
                                    cmd_aux + 4, aux_length, 500);
        if (ret < 0) return ret;

        uint16_t config_index = *(uint16_t*)(cmd_aux + 4);
        uint16_t base_freq_mhz = *((uint16_t*)(cmd_aux + 4) + 1);
        uint64_t base_freq = base_freq_mhz * 1000000ULL;
        
        printf("Config index: %u, Base freq: %u MHz\n", config_index, base_freq_mhz);

        if (base_freq % ctx->cur_samplerate != 0) {
            printf("Error: Cannot achieve samplerate %lu from base %lu\n", 
                ctx->cur_samplerate, base_freq);
            *(uint16_t*)(cmd_aux + 4) += 1;  // 尝试下一个配置
            ret = slogic_usb_control_write(ctx->dev_handle,
                                        SLOGIC16U3_CONTROL_OUT_REQ_REG_WRITE,
                                        SLOGIC16U3_R32_AUX + 4, 0x0000,
                                        cmd_aux + 4, aux_length, 500);
            if (ret < 0) return ret;
            continue;
        }

        uint32_t divider = base_freq / ctx->cur_samplerate;
        *((uint32_t*)(cmd_aux + 4) + 1) = divider - 1;
        
        printf("Setting divider: %u\n", divider - 1);
        
        ret = slogic_usb_control_write(ctx->dev_handle,
                                    SLOGIC16U3_CONTROL_OUT_REQ_REG_WRITE,
                                    SLOGIC16U3_R32_AUX + 4, 0x0000,
                                    cmd_aux + 4, aux_length, 500);
        if (ret < 0) return ret;

        // 验证采样率配置
        ret = slogic_usb_control_read(ctx->dev_handle,
                                    SLOGIC16U3_CONTROL_IN_REQ_REG_READ,
                                    SLOGIC16U3_R32_AUX + 4, 0x0000,
                                    cmd_aux + 4, aux_length, 500);
        if (ret < 0) return ret;

        break;
    }

    printf("Final samplerate config: index %u, base %u MHz, divider %u\n",
           *(uint16_t*)(cmd_aux + 4), 
           *((uint16_t*)(cmd_aux + 4) + 1),
           *((uint32_t*)(cmd_aux + 4) + 1));

    // 配置电压阈值
    *(uint32_t *)(cmd_aux) = 0x00000003;
    ret = slogic_usb_control_write(ctx->dev_handle,
                                 SLOGIC16U3_CONTROL_OUT_REQ_REG_WRITE,
                                 SLOGIC16U3_R32_AUX, 0x0000, cmd_aux, 4, 500);
    if (ret < 0) return ret;

    retry = 0;
    do {
        ret = slogic_usb_control_read(ctx->dev_handle,
                                    SLOGIC16U3_CONTROL_IN_REQ_REG_READ,
                                    SLOGIC16U3_R32_AUX, 0x0000, cmd_aux, 4, 500);
        if (ret < 0) return ret;
        
        printf("[%zu] Read voltage config: %08x\n", retry, *(uint32_t*)cmd_aux);
        retry++;
        
        if (retry > 5) {
            printf("Error: Timeout waiting for voltage configuration\n");
            return -1;
        }
    } while (!(cmd_aux[2] & 0x01));

    aux_length = (*(uint16_t *)cmd_aux) >> 9;
    printf("Voltage config length: %u\n", aux_length);
    
    ret = slogic_usb_control_read(ctx->dev_handle,
                                SLOGIC16U3_CONTROL_IN_REQ_REG_READ,
                                SLOGIC16U3_R32_AUX + 4, 0x0000,
                                cmd_aux + 4, aux_length, 500);
    if (ret < 0) return ret;

    printf("Current voltage AUX: %u %u %u %u %08x\n", 
           cmd_aux[0], cmd_aux[1], cmd_aux[2], cmd_aux[3], 
           *(uint32_t*)(cmd_aux + 4));

    // 设置电压阈值
    double avg_voltage = (ctx->voltage_threshold[0] + ctx->voltage_threshold[1]) / 2.0;
    *(uint32_t*)(cmd_aux + 4) = (uint32_t)(avg_voltage * 512.0 / 3333.0);
    
    printf("Setting voltage AUX: %u %u %u %u %08x (avg voltage: %.2fV)\n", 
           cmd_aux[0], cmd_aux[1], cmd_aux[2], cmd_aux[3], 
           *(uint32_t*)(cmd_aux + 4), avg_voltage);
           
    ret = slogic_usb_control_write(ctx->dev_handle,
                                 SLOGIC16U3_CONTROL_OUT_REQ_REG_WRITE,
                                 SLOGIC16U3_R32_AUX + 4, 0x0000,
                                 cmd_aux + 4, aux_length, 500);
    if (ret < 0) return ret;

    // 启动采集
    return slogic_usb_control_write(ctx->dev_handle,
                                  SLOGIC16U3_CONTROL_OUT_REQ_REG_WRITE,
                                  SLOGIC16U3_R32_CTRL, 0x0000,
                                  (uint8_t*)cmd_run, sizeof(cmd_run), 500);
}

// 停止采集
static int slogic16u3_stop_acquisition(libusb_device_handle *dev_handle)
{
    const uint8_t cmd_stop[] = { 0x00, 0x00, 0x00, 0x00 };
    return slogic_usb_control_write(dev_handle,
                                  SLOGIC16U3_CONTROL_OUT_REQ_REG_WRITE,
                                  SLOGIC16U3_R32_CTRL, 0x0000,
                                  (uint8_t*)cmd_stop, sizeof(cmd_stop), 500);
}

// 查找并打开设备
static libusb_device_handle* find_and_open_device(libusb_context *ctx)
{
    libusb_device **devs;
    libusb_device_handle *dev_handle = NULL;
    ssize_t cnt;
    
    cnt = libusb_get_device_list(ctx, &devs);
    if (cnt < 0) {
        printf("Error: Failed to get device list\n");
        return NULL;
    }
    
    for (ssize_t i = 0; i < cnt; i++) {
        libusb_device *dev = devs[i];
        struct libusb_device_descriptor desc;
        
        if (libusb_get_device_descriptor(dev, &desc) == 0) {
            if (desc.idVendor == USB_VID_SIPEED && desc.idProduct == USB_PID_SLOGIC16U3) {
                printf("Found SLogic16U3 device\n");
                
                int ret = libusb_open(dev, &dev_handle);
                if (ret == 0) {
                    // 尝试声明接口
                    if (libusb_claim_interface(dev_handle, 0) == 0) {
                        printf("Successfully opened and claimed device\n");
                        break;
                    } else {
                        printf("Warning: Could not claim interface\n");
                        libusb_close(dev_handle);
                        dev_handle = NULL;
                    }
                } else {
                    printf("Error: Could not open device: %s\n", libusb_error_name(ret));
                }
            }
        }
    }
    
    libusb_free_device_list(devs, 1);
    return dev_handle;
}

static uint64_t bytes_received_all = 0;

static void LIBUSB_CALL user_receive_transfer_cb(struct libusb_transfer *transfer)
{
    slogic16u3_context *ctx = (slogic16u3_context *)transfer->user_data;
    ctx->active_transfers--;

    if (transfer->status == LIBUSB_TRANSFER_COMPLETED) {
        // // Successful transfer
        // printf("Transfer completed: %d bytes received\n", transfer->actual_length);
        
        // // Process received data here
        // if (transfer->actual_length > 0) {
        //     printf("Data: ");
        //     for (int i = 0; i < transfer->actual_length && i < 16; i++) {
        //         printf("%02X ", transfer->buffer[i]);
        //     }
        //     printf("%s\n", transfer->actual_length > 16 ? "..." : "");
        // }

        if (transfer->actual_length > 0) {
            bytes_received_all += transfer->actual_length;
            static uint64_t last_report_time = 0;
            static uint64_t last_report_bytes = 0;
            struct timeval tv;
            gettimeofday(&tv, NULL);
            uint64_t current_time = tv.tv_sec * 1000 + tv.tv_usec / 1000; // 当前时间，单位毫秒
            if (current_time - last_report_time >= 1000) { // 每秒报告一次
                uint64_t bytes_this_interval = bytes_received_all - last_report_bytes;
                // double mbps = bytes_this_interval / 1000.0 / 1000.0; // 转换为MB/s
                double mbps = bytes_this_interval / 1000.0 / 1000.0 * 1000 / (current_time - last_report_time);
                double valid_mbps = (double)ctx->cur_samplerate / 1000000 * ctx->cur_samplechannel / 8;
                bool is_valid = mbps <= valid_mbps * 1.01 && mbps >= valid_mbps * 0.99;
                printf("Received: %lu bytes, Speed: %.2f MB/s(%.2f MB/s) is '%svalid'\n", bytes_received_all, mbps, valid_mbps, is_valid? "" : "in");
                // hexdump transfer->buffer n x 4(rev) x uint32_t(4bytes)
                if (ctx->cur_samplechannel == 16) {
                    for (int i = 0; i < transfer->actual_length && i < 64; i += 2) {
                        printf("%04X ", *(uint16_t *)(transfer->buffer + i));
                    }
                } else if (ctx->cur_samplechannel == 8) {
                    for (int i = 0; i < transfer->actual_length && i < 64; i += 1) {
                        printf("%02X ", *(uint8_t *)(transfer->buffer + i));
                    }
                } else if (ctx->cur_samplechannel == 4) {
                    for (int i = 0; i < transfer->actual_length && i < 64; i += 1) {
                        uint8_t s = *(uint8_t *)(transfer->buffer + i);
                        printf("%01X %01X ", s & 0x0F, (s >> 4) & 0x0F);
                    }
                }
                printf("%s\n", transfer->actual_length > 64 ? "..." : "");

                // === 新增：保存有效数据 ===
                if (is_valid) {
                    // 构造文件名
                    char filename[64];
                    snprintf(filename, sizeof(filename), "%uch_%luM_wave.bin", ctx->cur_samplechannel, ctx->cur_samplerate/1000000);
                    FILE *fp = fopen(filename, "ab");
                    if (fp) {
                        fwrite(transfer->buffer, 1, transfer->actual_length, fp);
                        fclose(fp);
                    } else {
                        perror("Failed to open wave file");
                    }
                }
                // === 新增结束 ===

                last_report_time = current_time;
                last_report_bytes = bytes_received_all;
            }
        }
        
    } else if (transfer->status == LIBUSB_TRANSFER_CANCELLED) {
        printf("Transfer cancelled\n");
        return;
    } else if (transfer->status == LIBUSB_TRANSFER_ERROR) {
        fprintf(stderr, "Transfer error\n");
    } else if (transfer->status == LIBUSB_TRANSFER_TIMED_OUT) {
        printf("Transfer timeout\n");
    } else if (transfer->status == LIBUSB_TRANSFER_STALL) {
        fprintf(stderr, "Transfer stalled\n");
    } else if (transfer->status == LIBUSB_TRANSFER_NO_DEVICE) {
        fprintf(stderr, "Device disconnected\n");
        ctx->should_stop = 1;
        return;
    } else if (transfer->status == LIBUSB_TRANSFER_OVERFLOW) {
        fprintf(stderr, "Transfer overflow\n");
    }
    
    // Resubmit the transfer if we should continue
    if (!ctx->should_stop) {
        int r = libusb_submit_transfer(transfer);
        if (r < 0) {
            fprintf(stderr, "Failed to resubmit transfer: %s\n", libusb_error_name(r));
        } else {
            ctx->active_transfers++;
        }
    }
}

int start_async_bulk_in_transfers(slogic16u3_context *ctx, unsigned char endpoint);
void stop_async_bulk_in_transfers(slogic16u3_context *ctx);

// Initialize and start the async transfers
int start_async_bulk_in_transfers(slogic16u3_context *ctx, unsigned char endpoint)
{
    int r;
    
    ctx->endpoint = endpoint;
    ctx->active_transfers = 0;
    ctx->should_stop = 0;
    
    // Create and submit all transfers
    for (int i = 0; i < NUM_TRANSFERS; i++) {
        struct libusb_transfer *transfer = libusb_alloc_transfer(0);
        if (!transfer) {
            fprintf(stderr, "Failed to allocate transfer %d\n", i);
            goto error;
        }
        
        unsigned char *buffer = (unsigned char *)malloc(TRANSFER_SIZE);
        if (!buffer) {
            fprintf(stderr, "Failed to allocate buffer for transfer %d\n", i);
            libusb_free_transfer(transfer);
            goto error;
        }
        
        libusb_fill_bulk_transfer(
            transfer,
            ctx->dev_handle,
            endpoint,
            buffer,
            TRANSFER_SIZE,
            user_receive_transfer_cb,
            ctx,
            BULK_TIMEOUT
        );
        
        ctx->transfers[i] = transfer;
        
        r = libusb_submit_transfer(transfer);
        if (r < 0) {
            fprintf(stderr, "Failed to submit transfer %d: %s\n", i, libusb_error_name(r));
            free(buffer);
            libusb_free_transfer(transfer);
            ctx->transfers[i] = NULL;
            if (i == 0)
                goto error;
            else
                break;
        }
        
        ctx->active_transfers++;
        printf("Started transfer %d\n", i);
    }
    
    return 0;

error:
    // Clean up any successfully created transfers
    stop_async_bulk_in_transfers(ctx);
    return -1;
}

// Stop and clean up all transfers
void stop_async_bulk_in_transfers(slogic16u3_context *ctx)
{
    ctx->should_stop = 1;
    
    // Cancel all active transfers
    for (int i = 0; i < NUM_TRANSFERS; i++) {
        if (ctx->transfers[i]) {
            libusb_cancel_transfer(ctx->transfers[i]);
        }
    }
    
    // Wait for all transfers to complete
    while (ctx->active_transfers > 0) {
        struct timeval tv = {0, 100000}; // 100ms timeout
        libusb_handle_events_timeout_completed(ctx->ctx, &tv, NULL);
    }
    
    // Free all transfers and buffers
    for (int i = 0; i < NUM_TRANSFERS; i++) {
        if (ctx->transfers[i]) {
            if (ctx->transfers[i]->buffer) {
                free(ctx->transfers[i]->buffer);
            }
            libusb_free_transfer(ctx->transfers[i]);
            ctx->transfers[i] = NULL;
        }
    }
}

// Main event handling loop
void event_loop(slogic16u3_context *ctx)
{
    struct timeval tv = {0, 100000}; // 100ms timeout
    
    while (!ctx->should_stop) {
        int r = libusb_handle_events_timeout_completed(ctx->ctx, &tv, NULL);
        if (r < 0) {
            if (r == LIBUSB_ERROR_INTERRUPTED) {
                continue; // Try again if interrupted
            }
            fprintf(stderr, "libusb_handle_events failed: %s\n", libusb_error_name(r));
            break;
        }
    }
}

#include <pthread.h>

// Thread function - must return void* and take void* argument
void* thread_function(void* arg) {
    slogic16u3_context *slogic_ctx = arg;
    printf("Thread %p is running\n", slogic_ctx->ctx);
    
    // Simulate some work
    event_loop(slogic_ctx);
    
    printf("Thread %p finished\n", slogic_ctx->ctx);
    return NULL;
}

#include <getopt.h>

// 定义长选项
static struct option long_options[] = {
    {"sr",    required_argument, 0, 's'},  // -sr 选项，需要参数
    {"ch",    required_argument, 0, 'c'},  // -ch 选项，需要参数
    {"volt",  required_argument, 0, 'v'},  // -volt 选项，需要参数
    {"timeout",required_argument, 0, 't'}, // -t 或 --timeout 选项
    {0, 0, 0, 0}                           // 选项数组结束标记
};

// 辅助函数：解析可能带有等号的参数
int parse_arg(const char *arg) {
    if (arg == NULL) return -1;
    
    // 检查是否有等号，如果有则使用等号后面的部分
    const char *equal_sign = strchr(arg, '=');
    if (equal_sign != NULL) {
        return atoi(equal_sign + 1);
    }
    
    // 否则直接转换
    return atoi(arg);
}


// 主测试函数
int main(int argc, char *argv[])
{
    int timeout_s = -1;

    libusb_device_handle *dev_handle = NULL;
    slogic16u3_context slogic_ctx = {0};

    // 设置默认值
    int sr = 200;       // 采样率默认值：200 MHz
    int ch = 16;        // 通道数默认值：16
    int volt = 3300;    // 电压默认值：3300 mV
    int timeout = 5; // 超时默认值：5 秒

    // 使用 getopt_long() 解析命令行选项
    for (int c, option_index = 0; (c = getopt_long(argc, argv, "s:c:v:t:",
                           long_options, &option_index)) != -1;) {
        switch (c) {
            case 's': {
                int val = parse_arg(optarg);
                if (val > 0) sr = val;  // 只接受正数值
                else {
                    fprintf(stderr, "错误: 所有选项都必须提供正数值\n");
                    return 1;
                }
                break;
            }
            case 'c': {
                int val = parse_arg(optarg);
                if (val > 0) ch = val;  // 只接受正数值
                else {
                    fprintf(stderr, "错误: 所有选项都必须提供正数值\n");
                    return 1;
                }
                break;
            }
            case 'v': {
                int val = parse_arg(optarg);
                if (val > 0) volt = val;  // 只接受正数值
                else {
                    fprintf(stderr, "错误: 所有选项都必须提供正数值\n");
                    return 1;
                }
                break;
            }
            case 't': {
                int val = parse_arg(optarg);
                if (val >= 0) timeout = val;  // 接受0或正数值（0表示无超时）
                break;
            }
            case '?':
                fprintf(stderr, "未知选项或缺少参数\n");
                fprintf(stderr, "用法: %s [选项]\n", argv[0]);
                fprintf(stderr, "选项:\n");
                fprintf(stderr, "  -s, --sr <MHz>    设置采样率 (单位: MHz)\n");
                fprintf(stderr, "  -c, --ch <num>    设置通道数\n");
                fprintf(stderr, "  -v, --volt <mV>   设置电压 (单位: mV)\n");
                fprintf(stderr, "  -t, --timeout <second>   设置超时 (单位: second)\n");
                fprintf(stderr, "参数格式支持: -sr 200 或 -sr=200\n");
                return 1;
            default:
                abort();  // 意外情况
        }
    }

    // 输出解析结果（包含默认值说明）
    printf("参数解析结果:\n");
    printf("  采样率: %d MHz %s\n", sr, (sr == 200) ? "(默认值)" : "");
    printf("  通道数: %d %s\n", ch, (ch == 16) ? "(默认值)" : "");
    printf("  电压: %d mV %s\n", volt, (volt == 3300) ? "(默认值)" : "");
    printf("  超时时间: %d s %s\n", timeout, (timeout == 5) ? "(默认值)" : (timeout == 0) ? "(Forever)" : "");

    timeout_s = timeout;
    if (!timeout_s) timeout_s = -1; // 无限大
    
    // 初始化libusb
    int ret = libusb_init(&slogic_ctx.ctx);
    if (ret < 0) {
        printf("Error: Failed to initialize libusb: %s\n", libusb_error_name(ret));
        return 1;
    }
    
    // 查找并打开设备
    dev_handle = find_and_open_device(slogic_ctx.ctx);
    if (!dev_handle) {
        printf("Error: Could not find or open SLogic16U3 device\n");
        libusb_exit(slogic_ctx.ctx);
        return 1;
    }
    
    slogic_ctx.dev_handle = dev_handle;
    slogic_ctx.cur_samplechannel = ch;  // 默认16通道
    slogic_ctx.cur_samplerate = 1000000ull * sr;  // 默认200MHz
    slogic_ctx.voltage_threshold[0] = volt;
    slogic_ctx.voltage_threshold[1] = volt;

    pthread_t thread;
    if (pthread_create(&thread, NULL, thread_function, &slogic_ctx) != 0) {
        perror("Failed to create thread");
        return 1;
    }
    
    printf("=== SLogic16U3 Test Program ===\n");
    
    // 测试设备复位
    printf("\n1. Testing device reset...\n");
    ret = slogic16u3_reset(dev_handle);
    if (ret < 0) {
        printf("Reset failed\n");
    } else {
        printf("Reset successful\n");
    }
    
    // 测试设置测试模式
    printf("\n2. Testing test mode configuration...\n");
    ret = slogic16u3_set_test_mode(dev_handle, 0x0);  // USB EMU_DATA模式
    if (ret < 0) {
        printf("Test mode configuration failed\n");
    } else {
        printf("Test mode configuration successful\n");
    }
    
    // 测试启动采集
    printf("\n3. Testing acquisition start...\n");

    // Start async transfers (replace with your endpoint)
    unsigned char endpoint = 0x82; // Typical bulk IN endpoint
    ret = start_async_bulk_in_transfers(&slogic_ctx, endpoint);
    if (ret < 0) {
        printf("Failed to start async transfers\n");
        goto _clean_up;
    }
    
    printf("Async transfers started. Press Ctrl+C to stop...\n");

    ret = slogic16u3_start_acquisition(&slogic_ctx);
    if (ret < 0) {
        printf("Acquisition start failed\n");
    } else {
        printf("Acquisition started successfully\n");
        
        // 等待一段时间
        printf("Acquiring data for %d seconds...\n", timeout_s);
        sleep(timeout_s);
        
        // 测试停止采集
        printf("\n4. Testing acquisition stop...\n");
        ret = slogic16u3_stop_acquisition(dev_handle);
        if (ret < 0) {
            printf("Acquisition stop failed\n");
        } else {
            printf("Acquisition stopped successfully\n");
        }
    }
    stop_async_bulk_in_transfers(&slogic_ctx);
    
    // 清理
_clean_up:
    // Wait for threads to finish
    pthread_join(thread, NULL);
    libusb_release_interface(dev_handle, 0);
    libusb_close(dev_handle);
    libusb_exit(slogic_ctx.ctx);
    
    printf("\n=== Test completed ===\n");
    return 0;
}